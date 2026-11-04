"""Saved posts stay server-owned and independent of local delivery/read state."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_coordinator as fixtures

from murmur import __main__ as cli
from murmur.backend import BackendError
from murmur.coordinator import Coordinator, actor_key
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity


class BookmarkBackend(fixtures.FakeBackend):
    """Native per-actor preferences, including unavailable content and lost writes."""

    def __init__(self):
        super().__init__()
        self.bookmarks = {}
        self.saved_calls = []
        self.saved_writes = []
        self.post_calls = []
        self.change_calls = []
        self.unavailable = {}
        self.saved_error = None
        self.saved_override = None
        self.post_override = {}
        self.lose_saved_response = False
        self.before_saved = None

    def changes(self, actor, cursor, generation):
        self.change_calls.append((actor, cursor, generation))
        return super().changes(actor, cursor, generation)

    def post(self, actor, post_id):
        self.post_calls.append((actor, post_id))
        if post_id in self.unavailable:
            raise BackendError("Post is unavailable", status=self.unavailable[post_id])
        if post_id in self.post_override:
            return dict(self.post_override[post_id])
        return super().post(actor, post_id)

    def saved(self, actor):
        self.saved_calls.append(actor)
        if self.saved_error:
            raise self.saved_error
        if self.saved_override is not None:
            return self.saved_override
        return [{"post_id": post_id, "unavailable": True}
                if post_id in self.unavailable else
                {"post_id": post_id, "post": dict(self.canonical[post_id]),
                 "conversation": self.descriptor(actor, self.canonical[post_id]["channel_id"])}
                for post_id in sorted(self.bookmarks.get(actor, set()))]

    def set_saved(self, actor, post_id, starred):
        self.saved_writes.append((actor, post_id, starred))
        if starred:
            self.post(actor, post_id)  # The server authorizes the exact target.
        if self.before_saved:
            self.before_saved()
        saved = self.bookmarks.setdefault(actor, set())
        if starred:
            saved.add(post_id)
        else:
            saved.discard(post_id)
        if self.lose_saved_response:
            self.lose_saved_response = False
            raise BackendError("Saved preference response lost", uncertain=True)
        return {"post_id": post_id, "starred": starred}


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        fixtures.CoordinatorTests.setUp(self)
        self.backend = BookmarkBackend()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        fixtures.hooks.initialize(self.ledger.db)

    tearDown = fixtures.CoordinatorTests.tearDown

    def state(self):
        return tuple(self.ledger.db.iterdump())

    def run_cli(self, *args, identity=None, output=None):
        stream = io.StringIO() if output is None else output
        with patch("murmur.__main__.config.ledger_path", return_value=self.path), \
             patch("murmur.__main__.config.load", return_value={"url": "http://fake", "token": "secret"}), \
             patch("murmur.__main__.Backend", return_value=self.backend), \
             patch("murmur.native.current_identity", return_value=identity or self.identity) as current, \
             redirect_stdout(stream):
            cli.main(list(args))
        current.assert_called_once_with()
        return json.loads(stream.getvalue())

    def test_read_last_then_star_keeps_a_zero_unread_conversation_visible(self):
        self.backend.add("request")
        selected = self.coordinator.read(self.actor, "build")
        self.ledger.mark_read(self.actor, [post["id"] for post in selected])
        self.assertEqual(self.coordinator.inbox(self.actor), [])

        self.assertEqual(self.coordinator.set_saved(self.actor, "request", True),
                         {"post_id": "request", "starred": True})
        inbox = self.coordinator.inbox(self.actor)
        self.assertEqual(len(inbox), 1)
        chat = inbox[0]
        self.assertEqual({key: chat[key] for key in ("id", "name", "kind", "role", "muted")},
                         {"id": "build", "name": "Build review", "kind": "channel", "role": "member", "muted": False})
        self.assertEqual(chat["unread_count"], 0)
        self.assertEqual(chat["starred_count"], 1)
        self.assertEqual([post["post_id"] for post in chat["starred"]], ["request"])
        self.coordinator.set_saved(self.actor, "request", False)
        self.assertEqual(self.coordinator.inbox(self.actor), [])
        self.assertEqual(self.ledger.outbounds(), [])
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_saved_root_and_exact_reply_keep_distinct_anchors_and_previews(self):
        root = self.backend.add("root")
        reply = self.backend.add("reply", root="root", when=102)
        reply["message"] = "  Exact\nreply  " + "x" * 200
        self.backend.chats.append({"id": "other", "name": "Other", "kind": "dm", "role": "member"})
        self.backend.add("unread-only", channel="other", when=103)
        self.coordinator.sync(self.actor)
        self.ledger.mark_read(self.actor, ["root", "reply"])
        for post_id in ("root", "reply"):
            self.coordinator.set_saved(self.actor, post_id, True)

        inbox = {chat["id"]: chat for chat in self.coordinator.inbox(self.actor)}
        self.assertEqual(set(inbox), {"build", "other"})
        self.assertEqual((inbox["other"]["unread_count"], inbox["other"]["starred_count"], inbox["other"]["starred"]),
                         (1, 0, []))
        saved_inbox = self.coordinator.inbox(self.actor, starred_only=True)
        self.assertEqual([chat["id"] for chat in saved_inbox], ["build"])
        self.assertEqual(saved_inbox[0]["starred_count"], 2)
        saved = {post["post_id"]: post for post in saved_inbox[0]["starred"]}
        for canonical in (root, reply):
            self.assertEqual(saved[canonical["id"]], {
                "post_id": canonical["id"], "root_id": canonical["root_id"],
                "author": canonical["author"], "preview": " ".join(canonical["message"].split())[:160],
                "create_at": canonical["create_at"],
            })
        self.backend.post_calls.clear()
        changes = list(self.backend.change_calls)
        self.assertEqual(self.coordinator.show(self.actor, "reply"), reply)
        self.assertEqual(self.backend.post_calls, [(self.actor, "reply")])
        self.assertEqual(self.backend.change_calls, changes)

    def test_saving_own_posts_and_uningested_history_creates_no_local_receipts(self):
        self.backend.add("mine", user="self")
        old = self.backend.add("old-history", when=10)
        self.backend.events.remove(old)
        self.coordinator.sync(self.actor)
        before = self.state()
        for post_id in ("mine", "old-history"):
            self.coordinator.set_saved(self.actor, post_id, True)
            self.assertEqual(self.coordinator.show(self.actor, post_id), self.backend.canonical[post_id])
        self.assertEqual(self.coordinator.inbox(self.actor)[0]["starred_count"], 2)
        self.assertEqual(self.coordinator.history(self.actor, "build")["posts"],
                         list(self.backend.canonical.values()))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ledger.unread_posts(self.actor), [])

    def test_restart_and_native_preference_changes_use_server_authority(self):
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        self.ledger.mark_read(self.actor, ["request"])
        self.coordinator.set_saved(self.actor, "request", True)
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.assertEqual(self.coordinator.inbox(self.actor)[0]["starred_count"], 1)

        self.backend.bookmarks[self.actor].clear()  # A native Saved Posts change.
        self.assertEqual(self.coordinator.inbox(self.actor), [])
        self.backend.bookmarks[self.actor].add("request")
        self.assertEqual(self.coordinator.inbox(self.actor, starred_only=True)[0]["starred_count"], 1)

    def test_child_actor_has_independent_saved_preferences(self):
        self.backend.add("parent-request")
        self.backend.add("child-request", when=102)
        self.coordinator.set_saved(self.actor, "parent-request", True)
        child = NativeIdentity("codex", "33333333-3333-4333-8333-333333333333",
                               parent_session_id=fixtures.SESSION, parent_known=True)
        self.coordinator.register(child, "Child")
        child_actor = actor_key(child)
        self.assertEqual(self.coordinator.inbox(child_actor, starred_only=True), [])
        self.coordinator.set_saved(child_actor, "child-request", True)
        for actor, expected in ((self.actor, "parent-request"), (child_actor, "child-request")):
            inbox = self.coordinator.inbox(actor, starred_only=True)
            self.assertEqual([post["post_id"] for post in inbox[0]["starred"]], [expected])

    def test_bookmark_commands_preserve_attention_attempts_budgets_and_outbound_state(self):
        self.backend.add("announced", causal={"chain_ids": ["exchange"]})
        self.coordinator.sync(self.actor)
        self.ledger.create_attempt(self.actor, "epoch", "announced-token")
        self.ledger.admit("announced-token", self.actor, "epoch", "finished-turn")
        self.ledger.complete(self.actor, "finished-turn", "epoch")
        self.ledger.mark_read(self.actor, ["announced"])
        self.backend.add("eligible", when=102)
        self.coordinator.sync(self.actor)
        self.ledger.create_attempt(self.actor, "epoch", "pending-token")
        self.ledger.prepare_outbound(self.actor, {"action": "post", "channel_id": "build", "message": "Held"}, "held")
        self.ledger.set_outbound("held", "uncertain", error="Response lost")
        before = self.state()

        actions = [
            lambda: self.coordinator.set_saved(self.actor, "announced", True),
            lambda: self.coordinator.inbox(self.actor, starred_only=True),
            lambda: self.coordinator.show(self.actor, "eligible"),
            lambda: self.coordinator.set_saved(self.actor, "announced", False),
            lambda: self.run_cli("star", "announced"),
            lambda: self.run_cli("inbox", "--starred"),
            lambda: self.run_cli("show", "eligible"),
            lambda: self.run_cli("unstar", "announced"),
        ]
        for index, action in enumerate(actions):
            with self.subTest(action=index):
                action()
                self.assertEqual(self.state(), before)
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.backend.operation_calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_lost_preference_response_can_retry_the_same_desired_state(self):
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        before = self.state()
        for starred in (True, False):
            with self.subTest(starred=starred):
                self.backend.lose_saved_response = True
                with self.assertRaises(BackendError):
                    self.coordinator.set_saved(self.actor, "request", starred)
                self.assertEqual("request" in self.backend.bookmarks[self.actor], starred)
                self.assertEqual(self.coordinator.set_saved(self.actor, "request", starred),
                                 {"post_id": "request", "starred": starred})
                self.assertEqual(self.state(), before)
        self.assertEqual(self.backend.saved_writes, [(self.actor, "request", value) for value in (True, True, False, False)])
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_read_and_cli_flush_do_not_clear_stars_or_consume_concurrent_arrivals(self):
        self.backend.add("request")
        self.coordinator.set_saved(self.actor, "request", True)
        self.backend.before_saved = lambda: (self.backend.add("concurrent", when=102), self.coordinator.sync(self.actor))
        self.coordinator.set_saved(self.actor, "request", True)
        self.backend.before_saved = None

        class BrokenOutput(io.StringIO):
            def flush(self):
                raise BrokenPipeError("Consumer disappeared")

        with self.assertRaises(BrokenPipeError):
            self.run_cli("read", "build", "--limit", "1", output=BrokenOutput())
        self.assertEqual([post["id"] for post in self.ledger.unread_posts(self.actor)], ["request", "concurrent"])
        self.assertEqual([post["id"] for post in self.run_cli("read", "build", "--limit", "1")["posts"]], ["request"])
        self.assertEqual([post["id"] for post in self.ledger.unread_posts(self.actor)], ["concurrent"])
        self.assertEqual(self.run_cli("show", "request"), {"post": self.backend.canonical["request"]})
        inbox = self.run_cli("inbox", "--starred")
        self.assertEqual((inbox[0]["unread_count"], inbox[0]["starred_count"]), (1, 1))
        self.assertEqual(self.backend.bookmarks[self.actor], {"request"})

    def test_saved_fetch_failure_remains_visible_and_show_failure_consumes_nothing(self):
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        before = self.state()
        for status in (None, 500):
            self.backend.saved_error = BackendError("Saved preferences unavailable", status=status)
            for starred_only in (False, True):
                with self.subTest(status=status, starred_only=starred_only), self.assertRaisesRegex(BackendError, "preferences unavailable"):
                    self.coordinator.inbox(self.actor, starred_only=starred_only)
        self.backend.saved_error = None
        for status in (403, 404, 500):
            self.backend.unavailable["request"] = status
            with self.subTest(show_status=status), self.assertRaises(BackendError):
                self.coordinator.show(self.actor, "request")
        self.assertEqual(self.state(), before)

    def test_unavailable_saved_ids_share_one_opaque_bucket_and_remain_removable(self):
        for post_id, status in (("lost-access", 403), ("missing", 404)):
            post = self.backend.add(post_id, channel="secret")
            self.backend.events.pop()
            post["message"] = "PRIVATE BODY"
            post["author"] = "PRIVATE AUTHOR"
            self.backend.bookmarks.setdefault(self.actor, set()).add(post_id)
            self.backend.unavailable[post_id] = status
        expected = {"id": None, "kind": "unavailable", "name": "Unavailable saved messages",
                    "unread_count": 0, "starred_count": 2,
                    "starred": [{"post_id": post_id, "unavailable": True} for post_id in ("lost-access", "missing")]}
        for starred_only in (False, True):
            inbox = self.coordinator.inbox(self.actor, starred_only=starred_only)
            self.assertEqual(len(inbox), 1)
            self.assertEqual({key: inbox[0][key] for key in expected}, expected)
            self.assertFalse({"channel_id", "author", "preview", "message", "post"} & inbox[0].keys())
            self.assertNotIn("PRIVATE", json.dumps(inbox))
            self.assertNotIn("secret", json.dumps(inbox))
        self.backend.post_calls.clear()
        for post_id in ("lost-access", "missing"):
            self.assertEqual(self.coordinator.set_saved(self.actor, post_id, False),
                             {"post_id": post_id, "starred": False})
        self.assertEqual(self.backend.post_calls, [])
        self.assertEqual(self.coordinator.inbox(self.actor), [])

    def test_show_and_saved_listing_reject_a_different_canonical_post_id(self):
        post = self.backend.add("requested")
        self.coordinator.sync(self.actor)
        before = self.state()
        self.backend.post_override["requested"] = dict(post, id="different")
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.show(self.actor, "requested")
        self.backend.post_override.clear()
        self.backend.saved_override = [{"post_id": "requested", "post": dict(post, id="different")}]
        with self.assertRaises((RuntimeError, ValueError)):
            self.coordinator.inbox(self.actor)
        self.assertEqual(self.state(), before)

    def test_cli_uses_verified_identity_and_exposes_no_actor_override(self):
        self.backend.add("request")
        self.assertEqual(self.run_cli("star", "request"), {"post_id": "request", "starred": True})
        self.assertEqual(self.run_cli("show", "request"), {"post": self.backend.canonical["request"]})
        self.assertEqual(self.run_cli("inbox", "--starred")[0]["starred_count"], 1)
        self.assertEqual(self.run_cli("unstar", "request"), {"post_id": "request", "starred": False})
        self.assertEqual(self.backend.saved_writes, [(self.actor, "request", True), (self.actor, "request", False)])
        for args in (("star", "request"), ("unstar", "request"), ("show", "request"), ("inbox", "--starred")):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cli.parser().parse_args([*args, "--actor", "somebody-else"])
            self.assertEqual(error.exception.code, 2)

        unknown = NativeIdentity("codex", "44444444-4444-4444-8444-444444444444", parent_known=True)
        self.backend.saved_writes.clear()
        self.backend.post_calls.clear()
        for command in ("star", "unstar", "show"):
            with self.subTest(unenrolled_command=command), self.assertRaises(LedgerError):
                self.run_cli(command, "request", identity=unknown)
        for operation in (lambda: self.coordinator.show(actor_key(unknown), "request"),
                          lambda: self.coordinator.set_saved(actor_key(unknown), "request", True),
                          lambda: self.coordinator.set_saved(actor_key(unknown), "request", False)):
            with self.assertRaises(LedgerError):
                operation()
        self.assertEqual(self.backend.saved_writes, [])
        self.assertEqual(self.backend.post_calls, [])


if __name__ == "__main__":
    unittest.main()
