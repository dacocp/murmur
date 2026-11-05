"""Local attention policy, irreversible user choices and crash-safe post intents."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur.ledger import Ledger, LedgerError
from murmur.protocol import ATTENTION_VERSION


class ThreadAttentionTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.path = Path(self.home.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.enroll("a", {"session_id": "a"}, "A", "user-a")
        self.chats = [{"id": "work", "name": "Work", "kind": "channel", "role": "member"}]
        self.ledger.sync_conversations("a", self.chats)
        self.sequence = 0

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def post(self, name, *, root="", channel="work", user="user-b", when=1):
        return {"id": name, "channel_id": channel, "root_id": root, "user_id": user,
                "author": user, "create_at": when, "update_at": when, "message": "canonical body"}

    def ingest(self, name, **kwargs):
        self.sequence += 1
        self.ledger.ingest("a", [self.post(name, **kwargs)], self.sequence, 1)

    def candidates(self):
        return [p["id"] for p in self.ledger.notification_candidates("a")]

    def mode(self, mode, after=None):
        return self.ledger.set_notification_mode("a", "work", mode,
            after_seq=self.sequence if after is None else after)

    def prepare(self, op="op", *, root="", after=None, attachment=False):
        payload = {"action": "post", "channel_id": "work", "root_id": root, "message": "" if attachment else "reply"}
        after = self.sequence if after is None else after
        if attachment:
            return self.ledger.prepare_attachment_outbound("a", payload,
                [{"path": "/not-read", "sha256": "digest"}], auto_follow_after_seq=after)
        return self.ledger.prepare_outbound("a", payload, op, auto_follow_after_seq=after)

    def finish(self, op="op", name="own", *, root=""):
        self.ledger.settle_outbound(op, "complete", {"post_id": name, "channel_id": "work"})
        return self.ledger.complete_auto_follow(op, self.post(name, root=root, user="user-a"))

    def test_thread_mode_is_prospective_and_unread_mail_stays_visible(self):
        self.ingest("root")
        self.ingest("old-reply", root="root")
        self.mode("threads")
        self.ledger.follow_thread("a", "work", "root", self.sequence)
        self.ingest("reply", root="root")
        self.ingest("other")
        self.assertEqual(self.candidates(), ["reply"])
        self.assertEqual(self.ledger.inbox("a")[0]["unread_count"], 4)
        self.assertEqual(self.ledger.inbox("a")[0]["notification_mode"], "threads")
        self.assertEqual(self.ledger.inbox("a")[0]["followed_thread_count"], 1)
        self.assertEqual([p["id"] for p in self.ledger.preview_candidates("a", "work")], ["reply"])

    def test_repeated_enable_and_follow_do_not_move_cutoffs(self):
        self.mode("threads", 2)
        self.ledger.follow_thread("a", "work", "root", 3)
        self.mode("threads", 100)
        self.ledger.follow_thread("a", "work", "root", 100)
        self.assertEqual(self.ledger.notification_mode("a", "work")["after_seq"], 2)
        self.assertEqual(self.ledger.thread_attention("a", "work", "root")["after_seq"], 3)

    def test_off_is_quiet_for_dms_mentions_and_existing_follow(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        self.mode("off")
        self.ingest("reply", root="root")
        self.assertEqual(self.candidates(), [])
        self.assertFalse(self.ledger.has_pending_attention("a"))
        self.assertEqual([p["id"] for p in self.ledger.preview_candidates("a", "work")], ["reply"])
        self.assertEqual(self.ledger.inbox("a")[0]["unread_count"], 1)
        self.assertEqual(len(self.ledger.following("a")), 1)
        self.mode("all")
        self.assertEqual(self.candidates(), [])
        self.ingest("later", root="root")
        self.assertEqual(self.candidates(), ["later"])

    def test_enabling_requires_boundary_and_full_membership(self):
        self.mode("off")
        with self.assertRaises(LedgerError):
            self.ledger.set_notification_mode("a", "work", "all")
        self.assertEqual(self.ledger.notification_mode("a", "work")["mode"], "off")
        self.ledger.sync_conversations("a", [dict(self.chats[0], role="observer")])
        with self.assertRaises(LedgerError):
            self.ledger.follow_thread("a", "work", "root", 1)
        dormant = self.ledger.set_notification_mode("a", "work", "all")
        self.assertEqual(dormant["mode"], "all")
        self.assertFalse(dormant["active"])
        self.ingest("observer-mail")
        self.assertEqual(self.candidates(), [])

    def test_slow_enable_cannot_override_repeated_off_on_another_connection(self):
        self.mode("off")
        fence = self.ledger.notification_mode("a", "work")
        with closing(Ledger(self.path)) as other:
            other.set_notification_mode("a", "work", "off")
        with self.assertRaises(LedgerError):
            self.ledger.set_notification_mode("a", "work", "all", after_seq=9,
                expected_revision=fence["revision"], expected_membership_revision=fence["membership_revision"])
        self.assertEqual(self.ledger.notification_mode("a", "work")["mode"], "off")

    def test_slow_mode_change_cannot_override_a_newer_same_mode_reaffirmation(self):
        for current, delayed in (("all", "threads"), ("threads", "all")):
            with self.subTest(current=current):
                self.mode(current, 5)
                captured = self.ledger.notification_mode("a", "work")
                with closing(Ledger(self.path)) as newer:
                    reaffirmed = newer.set_notification_mode("a", "work", current)
                self.assertEqual(reaffirmed["mode"], current)
                self.assertEqual(reaffirmed["after_seq"], captured["after_seq"])
                self.assertGreater(reaffirmed["revision"], captured["revision"])
                with self.assertRaisesRegex(LedgerError, "attention changed"):
                    self.ledger.set_notification_mode("a", "work", delayed, after_seq=10,
                        expected_revision=captured["revision"],
                        expected_membership_revision=captured["membership_revision"])
                actual = self.ledger.notification_mode("a", "work")
                self.assertEqual((actual["mode"], actual["after_seq"]), (current, captured["after_seq"]))

    def test_slow_follow_cannot_override_repeated_unfollow_or_off(self):
        self.ledger.unfollow_thread("a", "work", "root")
        for action in ("unfollow", "off"):
            with self.subTest(action=action):
                fence = self.ledger.thread_attention("a", "work", "root")
                with closing(Ledger(self.path)) as other:
                    if action == "unfollow":
                        other.unfollow_thread("a", "work", "root")
                    else:
                        other.set_notification_mode("a", "work", "off")
                with self.assertRaises(LedgerError):
                    self.ledger.follow_thread("a", "work", "root", 9,
                        expected_revision=fence["revision"], expected_attention_revision=fence["attention_revision"],
                        expected_membership_revision=fence["membership_revision"])
                self.assertFalse(self.ledger.thread_attention("a", "work", "root")["followed"])

    def test_slow_follow_normalization_is_fenced_by_unfollow_of_another_root(self):
        fence = self.ledger.notification_mode("a", "work")
        self.ledger.unfollow_thread("a", "work", "different-root")
        with self.assertRaises(LedgerError):
            self.ledger.follow_thread("a", "work", "root", 5,
                expected_attention_revision=fence["revision"], expected_membership_revision=fence["membership_revision"])

    def test_lost_post_response_retains_original_boundary_and_fast_reply_across_restart(self):
        self.mode("threads")
        self.prepare(after=0)
        self.ledger.claim_outbound("op")
        self.ingest("fast", root="own")
        self.assertEqual(self.candidates(), [])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.outbound("op")["state"], "uncertain")
        self.assertEqual(self.ledger.pending_auto_follows("a")[0]["after_seq"], 0)
        self.assertTrue(self.finish())
        self.assertEqual(self.candidates(), ["fast"])
        self.assertFalse(self.ledger.complete_auto_follow("op", self.post("own", user="user-a")))
        self.assertEqual(self.ledger.thread_attention("a", "work", "own")["after_seq"], 0)

    def test_completed_post_follow_recovers_after_crash_without_reposting(self):
        self.prepare()
        self.ledger.settle_outbound("op", "complete", {"post_id": "own", "channel_id": "work"})
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual([p["op_id"] for p in self.ledger.pending_auto_follows("a")], ["op"])
        self.assertTrue(self.ledger.complete_auto_follow("op", self.post("own", user="user-a")))
        self.assertEqual(len(self.ledger.outbounds("a")), 1)
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])

    def test_auto_follow_never_replays_backlog(self):
        self.mode("threads")
        self.ingest("old", root="root")
        self.prepare(root="root")
        self.ingest("fast", root="root")
        self.finish(root="root")
        self.assertEqual(self.candidates(), ["fast"])

    def test_own_file_only_post_and_intent_are_journaled_together(self):
        op = self.prepare(attachment=True)
        self.assertEqual(op["payload"]["message"], "")
        self.assertEqual(len(self.ledger.outbounds("a")), 2)
        self.assertEqual([p["op_id"] for p in self.ledger.pending_auto_follows("a")], [op["op_id"]])
        self.assertNotIn("auto_follow_after_seq", op["payload"])
        self.finish(op["op_id"])
        self.assertTrue(self.ledger.thread_attention("a", "work", "own")["followed"])

    def test_failure_during_intent_creation_rolls_back_the_entire_post_journal(self):
        with patch.object(self.ledger, "_prepare_auto_follow", side_effect=RuntimeError("disk failure")):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                self.prepare(attachment=True)
        self.assertEqual(self.ledger.outbounds("a"), [])
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])

    def test_known_failure_discards_intent_but_uncertainty_does_not(self):
        self.prepare()
        self.ledger.settle_outbound("op", "uncertain", error="lost")
        self.assertEqual(len(self.ledger.pending_auto_follows("a")), 1)
        self.ledger.settle_outbound("op", "failed", error="canonical rejection")
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])
        other = self.prepare(attachment=True)
        self.ledger.fail_attachment_outbound(other["op_id"], "cancelled before any write")
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])

    def test_new_root_late_proof_cannot_overwrite_explicit_unfollow(self):
        self.prepare()
        self.ledger.unfollow_thread("a", "work", "own")
        self.assertFalse(self.finish())
        self.assertEqual(self.ledger.outbound("op")["state"], "complete")
        self.assertTrue(self.ledger.thread_attention("a", "work", "own")["explicit_unfollow"])
        self.assertEqual(self.ledger.following("a"), [])

    def test_sticky_unfollow_survives_posting_and_explicit_refollow_is_prospective(self):
        self.ledger.unfollow_thread("a", "work", "root")
        self.prepare(root="root")
        self.finish(root="root")
        self.assertEqual(self.ledger.following("a"), [])
        self.ingest("old", root="root")
        self.mode("threads")
        self.ledger.follow_thread("a", "work", "root", self.sequence)
        self.ingest("new", root="root")
        self.assertEqual(self.candidates(), ["new"])

    def test_reverse_automatic_settlement_keeps_earliest_compatible_boundary(self):
        self.mode("threads")
        self.prepare("earlier", root="root", after=0)
        self.ledger.claim_outbound("earlier")  # First response is lost; success remains unresolved.
        self.ingest("fast-reply", root="root")
        self.prepare("later", root="root", after=1)
        self.assertTrue(self.finish("later", "later-own", root="root"))
        self.assertEqual(self.candidates(), [])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertTrue(self.finish("earlier", "earlier-own", root="root"))
        follow = self.ledger.thread_attention("a", "work", "root")
        self.assertEqual(follow["after_seq"], 0)
        self.assertEqual(follow["explicit_revision"], 0)
        self.assertEqual(self.candidates(), ["fast-reply"])
        self.assertEqual({o["state"] for o in self.ledger.outbounds("a")}, {"complete"})

    def test_explicit_follow_fences_older_automatic_boundary_without_moving_current_cutoff(self):
        self.mode("threads")
        self.prepare("earlier", root="root", after=0)
        self.prepare("later", root="root", after=1)
        self.assertTrue(self.finish("later", "later-own", root="root"))
        self.ledger.follow_thread("a", "work", "root", 99)  # Idempotent cutoff, new explicit intent.
        self.assertFalse(self.finish("earlier", "earlier-own", root="root"))
        self.assertEqual(self.ledger.thread_attention("a", "work", "root")["after_seq"], 1)
        self.assertEqual(self.ledger.outbound("earlier")["state"], "complete")

    def test_pre_journal_boundary_cannot_rewind_an_intervening_explicit_follow(self):
        self.mode("threads")
        captured_before_slow_response = 0
        self.ingest("old-reply", root="root")
        self.ledger.follow_thread("a", "work", "root", 1)
        self.prepare("slow", root="root", after=captured_before_slow_response)
        self.finish("slow", "own", root="root")
        self.assertEqual(self.ledger.thread_attention("a", "work", "root")["after_seq"], 1)
        self.assertEqual(self.candidates(), [])

    def test_late_auto_follow_does_not_replace_a_newer_explicit_refollow(self):
        self.prepare(root="root", after=0)
        self.ledger.unfollow_thread("a", "work", "root")
        self.ledger.follow_thread("a", "work", "root", 50)
        self.assertFalse(self.finish(root="root"))
        self.assertEqual(self.ledger.thread_attention("a", "work", "root")["after_seq"], 50)

    def test_leaving_cancels_active_and_pending_follows_and_refences_rejoin(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        self.prepare()
        old = self.ledger.notification_mode("a", "work")
        self.ledger.begin_leave("a", "work")
        self.assertEqual(self.ledger.following("a"), [])
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])
        self.ledger.sync_conversations("a", [])
        self.ledger.sync_conversations("a", self.chats)
        self.assertFalse(self.finish())
        with self.assertRaises(LedgerError):
            self.ledger.set_notification_mode("a", "work", "threads", after_seq=5,
                expected_revision=old["revision"], expected_membership_revision=old["membership_revision"])
        self.prepare("new-op", root="root", after=5)
        self.assertTrue(self.finish("new-op", "new-own", root="root"))
        self.assertEqual(self.ledger.thread_attention("a", "work", "root")["after_seq"], 5)

    def test_authoritative_membership_loss_also_cancels_intents(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        self.prepare(root="root")
        self.ledger.sync_conversations("a", [dict(self.chats[0], role="observer")])
        self.assertEqual(self.ledger.following("a"), [])
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])
        self.assertFalse(self.finish(root="root"))

    def test_off_retains_pending_follow_and_settles_quietly_without_reviving_old_arrivals(self):
        self.mode("threads")
        self.prepare(after=0)
        self.mode("off")
        self.ingest("during-off", root="own")
        self.assertEqual(len(self.ledger.pending_auto_follows("a")), 1)
        self.assertTrue(self.finish())
        self.assertEqual(len(self.ledger.following("a")), 1)
        self.assertEqual(self.candidates(), [])
        self.mode("threads")
        self.assertEqual(self.candidates(), [])
        self.ingest("after-enable", root="own")
        self.assertEqual(self.candidates(), ["after-enable"])

    def test_leave_journal_blocks_new_attention_and_known_rejection_releases_only_its_block(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        self.ledger.prepare_outbound("a", {"action": "leave", "channel_id": "work"}, "leave-1")
        self.ingest("during-leave", root="root")
        self.assertEqual(self.candidates(), [])
        self.assertEqual(self.ledger.following("a"), [])
        self.ledger.settle_outbound("leave-1", "failed", error="rejected")
        self.assertEqual(self.ledger.following("a"), [])
        self.assertEqual(self.candidates(), [])
        self.ingest("after-rejection")
        self.assertEqual(self.candidates(), ["after-rejection"])

    def test_late_leave_failure_cannot_release_a_newer_leave_or_override_off(self):
        for op in ("older", "newer"):
            self.ledger.prepare_outbound("a", {"action": "leave", "channel_id": "work"}, op)
        self.ledger.settle_outbound("older", "failed", error="late rejection")
        self.assertEqual(self.ledger.conversations("a")[0]["leave_op_id"], "newer")
        self.ingest("held")
        self.assertEqual(self.candidates(), [])
        self.mode("off")
        self.ledger.settle_outbound("newer", "failed", error="rejection")
        self.assertIsNone(self.ledger.conversations("a")[0]["leave_op_id"])
        self.assertEqual(self.ledger.notification_mode("a", "work")["mode"], "off")

    def test_completed_leave_stays_blocked_through_stale_snapshot_until_loss_then_rejoin(self):
        self.ledger.prepare_outbound("a", {"action": "leave", "channel_id": "work"}, "leave")
        self.ledger.settle_outbound("leave", "complete", {"channel_id": "work"})
        self.ledger.sync_conversations("a", self.chats)
        self.ingest("still-held")
        self.assertEqual(self.candidates(), [])
        self.ledger.sync_conversations("a", [])
        self.ledger.sync_conversations("a", self.chats)
        self.ingest("after-rejoin")
        self.assertEqual(self.candidates(), ["after-rejoin"])

    def test_leave_transaction_failure_does_not_cancel_follows_or_create_operation(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        original = self.ledger._revoke_channel_attention
        def interrupted(actor, channel):
            original(actor, channel)
            raise RuntimeError("interrupted write")
        with patch.object(self.ledger, "_revoke_channel_attention", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted write"):
                self.ledger.prepare_outbound("a", {"action": "leave", "channel_id": "work"}, "leave")
        self.assertIsNone(self.ledger.find_outbound("leave"))
        self.assertEqual(len(self.ledger.following("a")), 1)
        self.assertIsNone(self.ledger.conversations("a")[0]["leave_op_id"])

    def test_reopening_pre_feature_post_journal_does_not_invent_auto_follow(self):
        payload = {"action": "post", "channel_id": "work", "root_id": "", "message": "legacy"}
        self.ledger.prepare_outbound("a", payload, "legacy")
        self.ledger.prepare_outbound("a", payload, "legacy", auto_follow_after_seq=0)
        self.assertEqual(self.ledger.pending_auto_follows("a"), [])
        self.ledger.settle_outbound("legacy", "complete", {"post_id": "old-own", "channel_id": "work"})
        self.assertFalse(self.ledger.complete_auto_follow("legacy", self.post("old-own", user="user-a")))

    def test_bad_canonical_proof_never_follows_or_changes_completed_post(self):
        self.prepare()
        self.ledger.settle_outbound("op", "complete", {"post_id": "own", "channel_id": "work"})
        for changed in ({"channel_id": "secret"}, {"user_id": "user-b"}, {"id": "other"}, {"root_id": "wrong"}):
            with self.subTest(changed=changed), self.assertRaises(LedgerError):
                self.ledger.complete_auto_follow("op", dict(self.post("own", user="user-a"), **changed))
        self.assertEqual(self.ledger.following("a"), [])
        self.assertEqual(self.ledger.outbound("op")["state"], "complete")
        self.assertTrue(self.ledger.complete_auto_follow("op", self.post("own", user="user-a")))

    def test_immutable_first_seen_survives_edited_feed_entries(self):
        self.mode("threads", 0)
        self.ledger.follow_thread("a", "work", "root", 1)
        self.ingest("old", root="root")
        self.ledger.apply_feed("a", {"attention_version": ATTENTION_VERSION,
            "conversations": self.chats, "cursor": 10, "generation": 1, "reset": False, "more": False,
            "changes": [{"seq": 10, "first_seen_seq": 1, "notification_eligible": True,
                         "post": dict(self.post("old", root="root"), update_at=10)}]})
        self.assertEqual(self.ledger.unread_posts("a")[0]["first_seen_seq"], 1)
        self.assertEqual(self.candidates(), [])
        # A newly observed edit/backfill can carry a large change.seq for an old
        # original arrival. Qualification must use first_seen_seq on INSERT too.
        self.ledger.apply_feed("a", {"attention_version": ATTENTION_VERSION,
            "conversations": self.chats, "cursor": 12, "generation": 1, "reset": False, "more": False,
            "changes": [
                {"seq": 11, "first_seen_seq": 1, "notification_eligible": True,
                 "post": self.post("unseen-old", root="root")},
                {"seq": 12, "first_seen_seq": 2, "notification_eligible": True,
                 "post": self.post("unseen-new", root="root")}]})
        stored = {p["id"]: p["first_seen_seq"] for p in self.ledger.unread_posts("a")}
        self.assertEqual(stored, {"old": 1, "unseen-old": 1, "unseen-new": 2})
        self.assertEqual(self.candidates(), ["unseen-new"])

    def test_one_arrival_matches_all_and_follow_without_duplicate_notification(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        self.ingest("reply", root="root")
        self.assertEqual(self.candidates(), ["reply"])
        attempt = self.ledger.create_attempt("a", "epoch", "notice")
        self.assertEqual(attempt["post_ids"], ["reply"])
        self.assertIsNone(self.ledger.create_attempt("a", "epoch", "duplicate"))
        self.ledger.unfollow_thread("a", "work", "root")
        # All continues to cover the thread; unfollow changes thread routing only.
        self.assertTrue(self.ledger.has_pending_attention("a"))

    def test_unfollowed_thread_cannot_admit_a_frozen_idle_attempt_or_compaction_reminder(self):
        self.mode("threads")
        self.ledger.follow_thread("a", "work", "root", 0)
        self.ingest("reply", root="root")
        attempt = self.ledger.create_attempt("a", "epoch", "notice")
        self.assertTrue(self.ledger.has_pending_attention("a"))
        self.ledger.unfollow_thread("a", "work", "root")
        self.assertFalse(self.ledger.has_pending_attention("a"))
        with self.assertRaises(LedgerError):
            self.ledger.admit(attempt["token"], "a", "epoch", "turn")
        self.assertIsNone(self.ledger.unread_posts("a")[0]["announced_at"])

    def test_unfollow_between_busy_claim_and_native_handoff_prevents_emission(self):
        identity = {"provider": "codex", "session_id": "a"}
        self.ledger.update_identity("a", identity)
        self.mode("threads")
        self.ledger.follow_thread("a", "work", "root", 0)
        self.ledger.start_turn("a", "epoch", "turn", native_identity=identity)
        self.ingest("reply", root="root")
        attempt = self.ledger.claim_context("a", "epoch", "turn", "tool", native_identity=identity)
        self.ledger.unfollow_thread("a", "work", "root")
        self.assertFalse(self.ledger.mark_context_emitted(attempt["token"], "a", "epoch", "turn", "tool"))
        final = self.ledger.attempt(attempt["token"])
        self.assertIsNone(final["emitted_at"])
        self.assertEqual((final["status"], final["transport"]), ("retired", "rejected"))
        self.assertIsNone(self.ledger.unread_posts("a")[0]["announced_at"])
        self.assertEqual(self.ledger.active_turn("a")["turn_key"], "turn")

    def test_peer_unfollow_blocks_unsealed_frame_but_does_not_erase_a_real_late_receipt(self):
        # Reuse the existing synthetic protocol fixture rather than inventing a second peer seal format.
        import test_claude_peer_ledger as peer_fixture
        peer = peer_fixture.ClaudePeerLedgerTests()
        peer.setUp()
        try:
            peer.ledger.set_notification_mode(peer.actor, "build", "threads", after_seq=0)
            peer.ledger.follow_thread(peer.actor, "build", "initial", 0)
            peer.post()
            peer.start()
            unwritten = peer.claim()
            peer.ledger.unfollow_thread(peer.actor, "build", "initial")
            self.assertFalse(peer.seal(unwritten))
            self.assertIsNone(peer.ledger.attempt(unwritten["token"])["emitted_at"])
            peer.ledger.follow_thread(peer.actor, "build", "next", 1)
            peer.post("next")
            written = peer.claim()
            self.assertTrue(peer.seal(written))
            original = peer.ledger.attempt(written["token"])["emission"]
            peer.ledger.unfollow_thread(peer.actor, "build", "next")
            self.assertTrue(peer.confirm(written))
            self.assertEqual(peer.ledger.attempt(written["token"])["emission"], original)
            self.assertFalse(peer.ledger.thread_attention(peer.actor, "build", "next")["followed"])
            receipts = {r["id"]: r for r in peer.ledger.unread_posts(peer.actor)}
            self.assertIsNone(receipts["initial"]["announced_at"])
            self.assertIsNotNone(receipts["next"]["announced_at"])
        finally:
            peer.doCleanups()

    def test_read_and_completion_do_not_remove_follows(self):
        self.mode("threads")
        self.ledger.follow_thread("a", "work", "root", 0)
        self.ingest("reply", root="root")
        self.ledger.create_attempt("a", "epoch", "notice")
        self.ledger.admit("notice", "a", "epoch", "turn")
        self.ledger.mark_read("a", ["reply"])
        self.ledger.complete("a", "turn", "epoch")
        self.assertEqual([f["root_id"] for f in self.ledger.following("a")], ["root"])
        self.ingest("next", root="root")
        self.assertEqual(self.candidates(), ["next"])

    def test_manual_account_remains_quiet_even_with_enabled_follow(self):
        self.ledger.enroll("manual", {"session_id": "manual"}, "Manual", "user-m", mode="manual")
        self.ledger.sync_conversations("manual", self.chats)
        self.ledger.follow_thread("manual", "work", "root", 0)
        self.ledger.ingest("manual", [self.post("reply", root="root")], 1, 1)
        self.assertEqual(self.ledger.notification_candidates("manual"), [])
        self.assertEqual([p["id"] for p in self.ledger.preview_candidates("manual", "work")], ["reply"])
        self.assertIsNone(self.ledger.create_attempt("manual", "epoch"))
        self.assertFalse(self.ledger.has_pending_attention("manual"))
        self.assertEqual(self.ledger.inbox("manual")[0]["unread_count"], 1)

    def test_aliases_are_actor_owned_immutable_and_work_after_access_loss(self):
        self.ledger.remember_thread_alias("a", "old-reply", "work", "root")
        self.ledger.enroll("other", {"session_id": "other"}, "Other", "user-other")
        self.assertIsNone(self.ledger.resolve_thread_alias("other", "old-reply"))
        self.ledger.sync_conversations("a", [])
        self.assertEqual(self.ledger.resolve_thread_alias("a", "old-reply"), {"channel_id": "work", "root_id": "root"})
        self.assertTrue(self.ledger.unfollow_thread("a", "work", "root")["explicit_unfollow"])
        with self.assertRaises(LedgerError):
            self.ledger.remember_thread_alias("a", "old-reply", "work", "different")

    def child(self, name, choice="pending-and-channels"):
        self.ledger.enroll(name, {"session_id": name}, name, "user-" + name, onboarding=(choice, "a"))
        self.ledger.sync_conversations(name, self.chats)
        plan = self.ledger.onboarding(name)
        return plan["plan_id"], self.ledger.capture_pending(name, plan["plan_id"])

    def test_only_new_choice_four_snapshots_copy_independent_channel_attention(self):
        self.mode("threads")
        self.ledger.follow_thread("a", "work", "root", 0)
        plan, snapshot = self.child("child")
        self.assertEqual(snapshot["attention_version"], 1)
        copied = snapshot["channels"][0]
        self.assertEqual(copied["notification_mode"], "threads")
        self.assertEqual(copied["followed_threads"], ["root"])
        _, pending = self.child("pending", "pending")
        self.assertNotIn("attention_version", pending)
        self.assertEqual(pending["channels"], [])
        self.assertEqual(self.ledger.freeze_channel_attention_boundary("child", plan, "work", 10), 10)
        self.assertEqual(self.ledger.freeze_channel_attention_boundary("child", plan, "work", 99), 10)
        self.assertTrue(self.ledger.apply_channel_attention("child", plan, "work", "threads", ["root"], 10))
        self.assertFalse(self.ledger.apply_channel_attention("child", plan, "work", "threads", ["root"], 10))
        self.ledger.unfollow_thread("child", "work", "root")
        self.assertEqual(len(self.ledger.following("a")), 1)
        self.assertEqual(self.ledger.following("child"), [])
        self.assertEqual(self.ledger.notification_mode("child", "work")["after_seq"], 10)

    def test_explicit_child_preferences_and_unfollow_win_over_snapshot_application(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        plan, _ = self.child("child")
        self.ledger.freeze_channel_attention_boundary("child", plan, "work", 10)
        self.ledger.set_notification_mode("child", "work", "off")
        self.ledger.unfollow_thread("child", "work", "root")
        self.ledger.apply_channel_attention("child", plan, "work", "all", ["root"], 10)
        self.assertEqual(self.ledger.notification_mode("child", "work")["mode"], "off")
        self.assertEqual(self.ledger.following("child"), [])

    def test_onboarding_boundary_capture_cannot_stamp_a_new_membership_generation(self):
        self.ledger.follow_thread("a", "work", "root", 0)
        plan, _ = self.child("child")
        before_rpc = self.ledger.notification_mode("child", "work")["membership_revision"]
        self.ledger.sync_conversations("child", [])
        self.ledger.sync_conversations("child", self.chats)
        with self.assertRaisesRegex(LedgerError, "membership changed while capturing"):
            self.ledger.freeze_channel_attention_boundary("child", plan, "work", 10,
                expected_membership_revision=before_rpc)
        snapshot = self.ledger.onboarding("child")["snapshot"]
        self.assertNotIn("work", snapshot.get("attention_boundaries", {}))
        self.assertNotIn("work", snapshot.get("attention_membership_revisions", {}))
        self.assertEqual(self.ledger.following("child"), [])

    def test_onboarding_boundary_cannot_cross_child_leave_and_rejoin(self):
        plan, _ = self.child("child")
        self.ledger.freeze_channel_attention_boundary("child", plan, "work", 10)
        self.ledger.sync_conversations("child", [])
        self.ledger.sync_conversations("child", self.chats)
        with self.assertRaisesRegex(LedgerError, "membership changed"):
            self.ledger.apply_channel_attention("child", plan, "work", "all", [], 10)
        self.assertNotIn("attention:work", self.ledger.onboarding("child")["progress"])

    def test_old_frozen_onboarding_plan_is_not_rewritten_or_given_thread_semantics(self):
        self.ledger.enroll("child", {"session_id": "child"}, "Child", "user-child",
                           onboarding=("pending-and-channels", "a"))
        plan = self.ledger.onboarding("child")["plan_id"]
        frozen = '{"unread":[],"channels":[{"id":"work","muted":false}],"local_captured_at":7}'
        self.ledger.db.execute("UPDATE onboarding SET snapshot_json=? WHERE actor='child'", (frozen,))
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.capture_pending("child", plan), json.loads(frozen))
        self.assertEqual(self.ledger.db.execute("SELECT snapshot_json FROM onboarding WHERE actor='child'").fetchone()[0], frozen)
        with self.assertRaises(LedgerError):
            self.ledger.freeze_channel_attention_boundary("child", plan, "work", 9)

    def test_schema_four_upgrade_preserves_all_existing_evidence_without_inventing_sequences(self):
        self.ingest("read")
        self.ingest("unread")
        self.ledger.mark_read("a", ["read"])
        self.ledger.create_attempt("a", "epoch", "frozen")
        self.ledger.prepare_outbound("a", {"action": "post", "channel_id": "work", "message": "test"}, "old-op")
        self.mode("off")
        preserved = {table: [tuple(row) for row in self.ledger.db.execute("SELECT * FROM " + table)]
                     for table in ("actors", "posts", "attempts", "outbound", "onboarding")}
        self.ledger.close()
        with closing(sqlite3.connect(self.path)) as old:
            old.executescript("DROP TABLE auto_follow_intents; DROP TABLE thread_aliases; DROP TABLE thread_follows;")
            for column in ("notification_mode", "attention_after_seq", "attention_revision", "membership_revision", "leave_op_id"):
                old.execute("ALTER TABLE conversations DROP COLUMN " + column)
            old.execute("ALTER TABLE receipts DROP COLUMN first_seen_seq")
            receipts = old.execute("SELECT * FROM receipts").fetchall()
            old.execute("PRAGMA user_version=4")
            old.commit()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 5)
        for table, rows in preserved.items():
            self.assertEqual([tuple(row) for row in self.ledger.db.execute("SELECT * FROM " + table)], rows, table)
        self.assertEqual([tuple(row)[:-1] for row in self.ledger.db.execute("SELECT * FROM receipts")], receipts)
        self.assertTrue(all(row["first_seen_seq"] is None for row in self.ledger.db.execute("SELECT * FROM receipts")))
        self.assertEqual(self.ledger.notification_mode("a", "work")["mode"], "off")
        self.assertEqual(self.ledger.following("a"), [])


if __name__ == "__main__":
    unittest.main()
