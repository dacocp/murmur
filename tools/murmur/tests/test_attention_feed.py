"""Attention boundaries exercise the real ledger transaction and CLI inbox flow."""

from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_bookmarks as fixtures
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError, StaleFeedError


class AttentionFeedTests(unittest.TestCase):
    setUp = fixtures.BookmarkTests.setUp
    tearDown = fixtures.BookmarkTests.tearDown

    def page(self, posts=(), *, generation=1, reset=False, cursor=None, chats=None):
        entries = [{"seq": i + 1, "first_seen_seq": i + 1, "post": p, "notification_eligible": hint}
                   for i, (p, hint) in enumerate(posts)]
        return {"attention_version": 3, "conversations": deepcopy(self.backend.chats if chats is None else chats),
                "changes": entries, "cursor": len(entries) if cursor is None else cursor,
                "generation": generation, "more": False, "reset": reset}

    def snapshot(self):
        # INSERT OR IGNORE may reserve unused SQLite row numbers on replay;
        # receipt arrival IDs and every operational field must remain stable.
        return tuple(line for line in self.ledger.db.iterdump()
                     if not line.startswith('INSERT INTO "sqlite_sequence"'))

    def apply(self, page):
        self.ledger.apply_feed(self.actor, page)

    def test_quiet_history_and_edited_replays_cannot_become_fresh_arrivals(self):
        history = [self.backend.add("z-history-" + str(i), when=101) for i in range(31)]
        history.append(self.backend.add("future-history", when=9999))
        fresh = self.backend.add("a-fresh", when=101)
        page = self.page([(p, False) for p in history] + [(fresh, True)], generation=2, reset=True)
        self.apply(page)
        unread = self.ledger._unread(self.actor)
        self.assertEqual(len(unread), 33)
        self.assertTrue(all(p["announced_at"] is None for p in unread))
        self.assertEqual([p["id"] for p in self.ledger._unread(self.actor, eligible=True)], ["a-fresh"])
        self.assertEqual(self.ledger.preview_candidates(self.actor, "build")[0]["id"], "a-fresh")
        attempt = self.ledger.create_attempt(self.actor, "fixture", token="fresh-wake")
        self.assertEqual(attempt["post_ids"], ["a-fresh"])
        self.ledger.admit(attempt["token"], self.actor, "fixture", "actual-turn")
        # Admission must not remove the work preview just because it is announced.
        self.assertEqual(self.ledger.preview_candidates(self.actor, "build")[0]["id"], "a-fresh")
        before = {p["id"]: (p["arrival"], p["read_at"], p["announced_at"], p["notification_eligible"])
                  for p in self.ledger._unread(self.actor)}
        edited = dict(history[0], update_at=200)
        self.apply(self.page([(edited, False), (fresh, True)], generation=3, reset=True, cursor=40))
        after = {p["id"]: (p["arrival"], p["read_at"], p["announced_at"], p["notification_eligible"])
                 for p in self.ledger._unread(self.actor)}
        self.assertEqual(after, before)
        self.ledger.mark_read(self.actor, [fresh["id"]])
        self.assertEqual(len(self.ledger._unread(self.actor)), 32)
        self.assertEqual(self.ledger._unread(self.actor, eligible=True), [])
        self.assertTrue(all(p["announced_at"] is None for p in self.ledger.unread_posts(self.actor)))

    def test_invalid_late_entry_or_empty_protocol_cannot_partially_replace_state(self):
        first, last = self.backend.add("first"), self.backend.add("last")
        valid = self.page([(first, True), (last, False)])
        valid["conversations"][0]["name"] = "Must not commit on failure"
        invalid = []
        for version in (None, True, "3", 1, 2, 4):
            item = self.page(); item["attention_version"] = version; invalid.append(item)
        missing = self.page(); del missing["attention_version"]; invalid.append(missing)
        for flag in (None, 0, 1, "false"):
            item = deepcopy(valid); item["changes"][-1]["notification_eligible"] = flag; invalid.append(item)
        item = deepcopy(valid); del item["changes"][-1]["notification_eligible"]; invalid.append(item)
        item = deepcopy(valid); item["changes"][-1]["post"]["channel_id"] = "not-in-snapshot"; invalid.append(item)
        item = self.page(); item["conversations"] *= 2; invalid.append(item)
        item = deepcopy(valid); item["changes"][-1]["seq"] = 1; invalid.append(item)
        for first_seen in (None, True, 0, 3):
            item = deepcopy(valid); item["changes"][-1]["first_seen_seq"] = first_seen; invalid.append(item)
        item = deepcopy(valid); item["reset"] = 1; invalid.append(item)
        item = deepcopy(valid); item["generation"] = 2; invalid.append(item)
        item = self.page(); item["more"] = 0; invalid.append(item)
        before = self.snapshot()
        for item in invalid:
            with self.subTest(page=item), self.assertRaises(LedgerError):
                self.apply(item)
            self.assertEqual(self.snapshot(), before)

    def test_semantic_identity_failure_rolls_back_access_suppression_and_first_post(self):
        old = self.backend.add("already-known")
        self.apply(self.page([(old, True)], cursor=1))
        chats = [{"id": "elsewhere", "name": "Wrong room", "kind": "dm", "role": "member"}]
        fresh = dict(self.backend.add("new"), channel_id="elsewhere")
        altered = dict(old, channel_id="elsewhere")
        before = self.snapshot()
        with self.assertRaisesRegex(LedgerError, "identity changed"):
            self.apply(self.page([(fresh, True), (altered, False)], chats=chats,
                                 generation=2, reset=True, cursor=3))
        self.assertEqual(self.snapshot(), before)

    def test_generation_is_checked_after_an_overlapping_writer_commits(self):
        old_response = self.page(cursor=1)
        newer = self.page([(self.backend.add("current-request"), True)],
                          generation=2, reset=True, cursor=3)
        original_write = self.ledger._write
        committed = []

        @contextmanager
        def late_write():
            concurrent = Ledger(self.path, clock=lambda: 100)
            try:
                concurrent.apply_feed(self.actor, newer)
            finally:
                concurrent.close()
            committed.append(self.snapshot())
            with original_write():
                yield

        with patch.object(self.ledger, "_write", late_write), self.assertRaises(StaleFeedError):
            self.apply(old_response)
        self.assertEqual(self.snapshot(), committed[0])
        with self.assertRaises(StaleFeedError):
            self.apply(self.page(generation=2, cursor=2))
        with self.assertRaisesRegex(LedgerError, "explicit reset"):
            self.apply(self.page(generation=3, cursor=4))
        self.assertEqual(self.ledger._unread(self.actor, eligible=True)[0]["id"], "current-request")

    def test_revocation_and_explicit_preferences_survive_metadata_replay(self):
        post = self.backend.add("request")
        self.apply(self.page([(post, True)], cursor=1))
        self.ledger.set_mute(self.actor, "build", True)
        self.apply(self.page([(post, True)], generation=2, reset=True, cursor=2))
        self.assertTrue(self.ledger.conversations(self.actor)[0]["mute_explicit"])
        self.assertEqual(self.ledger.unread_posts(self.actor)[0]["notification_eligible"], 0)
        self.ledger.set_mute(self.actor, "build", False)
        self.apply(self.page([(post, True)], generation=3, reset=True, cursor=3))
        self.assertEqual(self.ledger._unread(self.actor, eligible=True), [])
        self.apply(self.page(chats=[], generation=4, reset=True, cursor=3))
        self.assertEqual(self.ledger.conversations(self.actor), [])
        self.apply(self.page([(post, True)], generation=5, reset=True, cursor=4))
        self.assertFalse(self.ledger.conversations(self.actor)[0]["muted"])
        self.assertTrue(self.ledger.conversations(self.actor)[0]["mute_explicit"])
        self.assertEqual(self.ledger._unread(self.actor, eligible=True), [])

    def test_sync_uses_one_coherent_feed_and_retries_only_bounded_stale_responses(self):
        fresh = self.backend.add("new-chat-request", channel="human-dm")
        chats = [{"id": "human-dm", "name": "Human request", "kind": "dm", "role": "member"}]
        calls = []
        responses = [self.page(generation=0), self.page([(fresh, True)], chats=chats,
                                                      generation=2, reset=True)]
        def changes(actor, cursor, generation):
            calls.append((actor, cursor, generation))
            return responses.pop(0)
        self.backend.changes = changes
        with patch.object(self.backend, "conversations", side_effect=AssertionError("No separate metadata RPC")):
            self.coordinator.sync(self.actor)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.ledger._unread(self.actor, eligible=True)[0]["id"], fresh["id"])
        self.backend.changes = lambda *_args: calls.append("stale") or self.page(generation=1)
        before = self.snapshot()
        with self.assertRaisesRegex(StaleFeedError, "restore/reconciliation"):
            self.coordinator.sync(self.actor)
        self.assertEqual(calls.count("stale"), 3)
        self.assertEqual(self.snapshot(), before)

    def test_inbox_fresh_anchor_deleted_fallback_is_quiet_and_bounded(self):
        history = [self.backend.add("z-history-" + str(i), when=9999) for i in range(31)]
        live = self.backend.add("a-live", when=101)
        gone = self.backend.add("a-deleted", when=101)
        page = self.page([(p, False) for p in history] + [(live, True), (gone, True)])
        self.backend.changes = lambda *_args: deepcopy(page)
        self.backend.unavailable[gone["id"]] = 404
        self.coordinator.sync(self.actor)
        before = self.snapshot()
        inbox = self.coordinator.inbox(self.actor)
        self.assertEqual((inbox[0]["latest_post_id"], inbox[0]["unread_count"]), (live["id"], 33))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p for _, p in self.backend.post_calls], [gone["id"], live["id"]])
        self.backend.unavailable[gone["id"]] = 403
        with self.assertRaises(BackendError):
            self.coordinator.inbox(self.actor)
        self.assertEqual(self.snapshot(), before)
        self.ledger.set_mute(self.actor, "build", True)
        self.assertEqual(self.ledger.preview_candidates(self.actor, "build")[0]["create_at"], 9999)

    def test_transport_always_requests_attention_protocol(self):
        backend = Backend("http://fixture.invalid", "fixture")
        with patch.object(backend, "request", return_value={}) as request:
            backend.changes(self.actor, 4, 2)
        request.assert_called_once_with("GET", "/changes", actor=self.actor, cursor=4,
                                        generation=2, limit=100, attention_version=3)


if __name__ == "__main__":
    unittest.main()
