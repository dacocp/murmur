"""Canonical boundaries, CAS choices and journaled send-follow recovery."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from murmur import __main__ as cli, hooks
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from test_attachments_client import AttachmentBackend
from test_open_channels import OpenBackend
import test_coordinator as fixtures


class AttentionBackend(fixtures.FakeBackend):
    def __init__(self):
        super().__init__()
        self.capability = {"version": 1}
        self.boundaries = []
        self.before_boundary = None
        self.before_post = None
        self.reject_post = False
        self.reject_leave = False
        self.return_wrong_post = False

    def request(self, method, path):
        if (method, path) == ("GET", "/health"):
            return {"capabilities": {"attention_boundaries": self.capability}}
        return super().request(method, path)

    def attention_boundary(self, actor, channel):
        result = super().attention_boundary(actor, channel)
        self.boundaries.append((actor, channel, result["after_seq"]))
        callback, self.before_boundary = self.before_boundary, None
        if callback:
            callback()
        return result

    def post(self, actor, post_id):
        callback, self.before_post = self.before_post, None
        if callback:
            callback()
        return super().post(actor, post_id)

    def mutate(self, action, actor, op_id, **payload):
        if action == "leave":
            self.mutation_calls.append((action, actor, op_id, payload))
            if self.before_mutation:
                self.before_mutation(op_id)
            if self.reject_leave:
                raise BackendError("Leave rejected", status=403)
            result = {"ok": True}
            self.operations[op_id] = {"status": "complete", "result": result}
            self.chats = [c for c in self.chats if c["id"] != payload["channel_id"]]
            return result
        if self.reject_post:
            self.mutation_calls.append((action, actor, op_id, payload))
            raise BackendError("Post rejected", status=403)
        result = super().mutate(action, actor, op_id, **payload)
        if self.return_wrong_post:
            result = {"post": {**result["post"], "user_id": "another-account"}}
        return result

    def threads(self, actor, channel, limit, before):
        return {"conversation": self.descriptor(actor, channel), "threads": [
            {"id": p["id"], "channel_id": channel, "preview": "Thread"}
            for p in self.canonical.values() if p["channel_id"] == channel and not p["root_id"]][:limit],
            "next_before": None}


class ThreadNotificationRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixtures.CoordinatorTests.setUp(self)
        hooks.initialize(self.ledger.db)
        self.backend = AttentionBackend()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)

    tearDown = fixtures.CoordinatorTests.tearDown

    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch("murmur.native.current_identity", return_value=self.identity), redirect_stdout(output):
            cli.main(list(arguments))
        return json.loads(output.getvalue())

    def restart(self):
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)

    def inherited_child(self):
        self.backend = OpenBackend()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        parent = NativeIdentity("codex", "44444444-4444-4444-8444-444444444444", parent_known=True)
        self.coordinator.register(parent, "Parent")
        child = NativeIdentity("codex", "33333333-3333-4333-8333-333333333333",
                               parent_session_id=parent.session_id, parent_known=True)
        self.coordinator.register(child, "Child")
        return child

    def test_modes_capture_future_boundary_once_and_off_is_offline(self):
        self.backend.add("old")
        self.coordinator.sync(self.actor)
        original = self.coordinator.notify(self.actor, "build", "threads")
        self.assertEqual(original["after_seq"], 1)
        self.backend.add("later")
        self.assertEqual(self.coordinator.notify(self.actor, "build", "threads")["after_seq"], 1)
        self.assertEqual(len(self.backend.boundaries), 1)
        with patch.object(self.backend, "request", side_effect=AssertionError("Off must stay local")):
            self.assertEqual(self.run_cli("mute", "build"), {"id": "build", "muted": True})
        self.assertEqual(self.ledger.notification_mode(self.actor, "build")["mode"], "off")
        self.assertEqual(self.run_cli("unmute", "build"), {"id": "build", "muted": False})
        self.assertEqual(self.ledger.notification_mode(self.actor, "build")["after_seq"], 2)
        self.coordinator.sync(self.actor)
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])
        self.backend.add("new-arrival")
        self.coordinator.sync(self.actor)
        self.assertEqual([p["id"] for p in self.ledger.notification_candidates(self.actor)], ["new-arrival"])

    def test_follow_normalizes_reply_and_repeats_offline_without_rebasing(self):
        self.backend.add("root")
        self.backend.add("reply", root="root")
        value = self.coordinator.follow(self.actor, "reply")
        self.assertEqual((value["root_id"], value["after_seq"]), ("root", 2))
        count = len(self.backend.boundaries)
        with patch.object(self.backend, "post", side_effect=AssertionError("Known follow is local")), \
                patch.object(self.backend, "request", side_effect=AssertionError("Known follow is local")):
            self.assertEqual(self.coordinator.follow(self.actor, "reply")["after_seq"], 2)
            self.assertEqual(self.coordinator.following(self.actor)[0]["root_id"], "root")
            self.assertFalse(self.coordinator.unfollow(self.actor, "reply")["followed"])
        self.assertEqual(len(self.backend.boundaries), count)

    def test_slow_enable_cannot_override_a_later_off(self):
        self.backend.before_boundary = lambda: self.coordinator.notify(self.actor, "build", "off")
        with self.assertRaisesRegex(LedgerError, "changed"):
            self.coordinator.notify(self.actor, "build", "threads")
        self.assertEqual(self.ledger.notification_mode(self.actor, "build")["mode"], "off")

    def test_same_enabled_mode_reaffirmation_fences_a_slower_different_choice(self):
        self.backend.before_boundary = lambda: self.coordinator.notify(self.actor, "build", "all")
        with self.assertRaisesRegex(LedgerError, "changed"):
            self.coordinator.notify(self.actor, "build", "threads")
        value = self.ledger.notification_mode(self.actor, "build")
        self.assertEqual((value["mode"], value["after_seq"]), ("all", 0))
        self.assertEqual(len(self.backend.boundaries), 1)

    def test_slow_normalization_cannot_override_later_off_or_unfollow(self):
        self.backend.add("root")
        self.backend.before_post = lambda: self.coordinator.notify(self.actor, "build", "off")
        with self.assertRaisesRegex(LedgerError, "changed"):
            self.coordinator.follow(self.actor, "root")
        self.assertEqual(self.coordinator.following(self.actor), [])
        self.backend.before_post = lambda: self.coordinator.unfollow(self.actor, "root")
        with self.assertRaisesRegex(LedgerError, "changed"):
            self.coordinator.follow(self.actor, "root")
        self.assertTrue(self.ledger.thread_attention(self.actor, "build", "root")["explicit_unfollow"])

    def test_repeated_off_fences_an_inflight_follow_even_without_a_visible_mode_change(self):
        self.backend.add("root")
        self.coordinator.notify(self.actor, "build", "off")
        self.backend.before_boundary = lambda: self.coordinator.notify(self.actor, "build", "off")
        with self.assertRaisesRegex(LedgerError, "changed"):
            self.coordinator.follow(self.actor, "root")
        self.assertEqual(self.coordinator.following(self.actor), [])

    def test_leave_intent_fences_a_slow_follow_before_http(self):
        self.backend.add("root")
        self.backend.before_boundary = lambda: self.coordinator.mutate(self.actor, "leave", channel_id="build")
        with self.assertRaises(LedgerError):
            self.coordinator.follow(self.actor, "root")
        self.assertEqual(self.coordinator.following(self.actor), [])
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])

    def test_rejected_leave_clears_its_block_without_restoring_follows_or_overriding_off(self):
        self.backend.add("root")
        self.coordinator.follow(self.actor, "root")
        self.backend.reject_leave = True

        def off_before_rejection(_op_id):
            self.assertEqual(self.coordinator.following(self.actor), [])
            self.coordinator.notify(self.actor, "build", "off")

        self.backend.before_mutation = off_before_rejection
        with self.assertRaises(BackendError):
            self.coordinator.mutate(self.actor, "leave", channel_id="build")
        chat = self.ledger.conversations(self.actor)[0]
        self.assertIsNone(chat["leave_op_id"])
        self.assertEqual(chat["notification_mode"], "off")
        self.assertEqual(self.coordinator.following(self.actor), [])

    def test_unfollowing_new_root_before_response_cannot_be_restored_by_completion(self):
        def unfollow_before_response(op_id):
            post_id = "reply-" + op_id
            self.backend.add(post_id, user="self")
            self.coordinator.unfollow(self.actor, post_id)

        self.backend.before_mutation = unfollow_before_response
        result = self.coordinator.send(self.actor, "build", "Root")
        self.assertEqual(self.ledger.outbounds(self.actor)[0]["state"], "complete")
        self.assertTrue(self.ledger.thread_attention(self.actor, "build", result["post"]["id"])["explicit_unfollow"])
        self.assertEqual(self.coordinator.following(self.actor), [])

    def test_fast_reply_is_eligible_after_canonical_send_completion_with_original_boundary(self):
        self.coordinator.notify(self.actor, "build", "threads")

        def reply_before_response(op_id):
            intent = self.ledger.pending_auto_follows(self.actor)[0]
            self.assertEqual((intent["op_id"], intent["after_seq"]), (op_id, 0))
            self.backend.add("fast-reply", root="reply-" + op_id)
            self.coordinator.sync(self.actor)
            self.assertEqual(self.ledger.notification_candidates(self.actor), [])

        self.backend.before_mutation = reply_before_response
        result = self.coordinator.send(self.actor, "build", "Please review")
        self.assertEqual(self.coordinator.following(self.actor)[0]["root_id"], result["post"]["id"])
        self.assertEqual([p["id"] for p in self.ledger.notification_candidates(self.actor)], ["fast-reply"])
        self.assertFalse(any(k.startswith("auto_follow") for k in self.backend.mutation_calls[0][3]))

    def test_uncertain_send_reopens_and_uses_operation_proof_without_refetch_or_repost(self):
        self.coordinator.notify(self.actor, "build", "threads")
        self.backend.lose_response = True
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "Original intent")
        operation = self.ledger.outbounds(self.actor)[0]
        cutoff = self.ledger.pending_auto_follows(self.actor)[0]["after_seq"]
        post_id = "reply-" + operation["op_id"]
        self.backend.operations[operation["op_id"]] = {"status": "complete", "result": {
            "post_id": post_id, "channel_id": "build"}}
        self.backend.add("fast-reply", root=post_id)
        self.restart()
        with patch.object(self.backend, "post", side_effect=AssertionError("Deleted post need not be refetched")):
            self.coordinator.reconcile_outbound(self.actor)
        follow = self.coordinator.following(self.actor)[0]
        self.assertEqual((follow["root_id"], follow["after_seq"]), (post_id, cutoff))
        self.assertEqual(len(self.backend.mutation_calls), 1)
        self.assertEqual(self.ledger.pending_auto_follows(self.actor), [])

    def test_reaffirmed_follow_fences_older_uncertain_send_without_http_or_rebasing(self):
        self.backend.add("root")
        self.backend.events.clear()
        self.coordinator.notify(self.actor, "build", "threads")
        self.backend.lose_response = True
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "First reply", root="root")
        self.backend.add("old-fast-reply", root="root")
        self.backend.lose_response = False
        self.coordinator.send(self.actor, "build", "Second reply", root="root")
        self.assertEqual(self.coordinator.following(self.actor)[0]["after_seq"], 1)
        with patch.object(self.backend, "request", side_effect=AssertionError("Reaffirmation is local")), \
                patch.object(self.backend, "post", side_effect=AssertionError("No canonical refetch")):
            self.assertEqual(self.coordinator.follow(self.actor, "root")["after_seq"], 1)
        self.restart()
        self.coordinator.reconcile_outbound(self.actor)
        self.coordinator.sync(self.actor)
        self.assertEqual(self.coordinator.following(self.actor)[0]["after_seq"], 1)
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])
        self.assertEqual(len(self.backend.mutation_calls), 2)

    def test_completed_outbound_crash_window_repairs_locally_and_legacy_journals_do_not_follow(self):
        operation = self.ledger.prepare_outbound(self.actor,
            {"action": "post", "channel_id": "build", "message": "Sent", "root_id": ""},
            auto_follow_after_seq=0)
        self.ledger.set_outbound(operation["op_id"], "complete", result={"post_id": "created-root", "channel_id": "build"})
        legacy = self.ledger.prepare_outbound(self.actor,
            {"action": "post", "channel_id": "build", "message": "Legacy", "root_id": ""})
        self.ledger.set_outbound(legacy["op_id"], "complete", result={"post_id": "legacy-root", "channel_id": "build"})
        self.restart()
        with patch.object(self.backend, "operation", side_effect=AssertionError("Completion is already sealed")), \
                patch.object(self.backend, "post", side_effect=AssertionError("No content read")):
            self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual([f["root_id"] for f in self.coordinator.following(self.actor)], ["created-root"])
        self.assertEqual(self.backend.mutation_calls, [])

    def test_explicit_unfollow_is_sticky_while_posting_still_completes(self):
        self.backend.add("root")
        self.coordinator.follow(self.actor, "root")
        self.coordinator.unfollow(self.actor, "root")
        result = self.coordinator.send(self.actor, "build", "Reply", root="root")
        self.assertEqual(result["post"]["root_id"], "root")
        self.assertEqual(self.coordinator.following(self.actor), [])
        self.assertEqual(self.ledger.outbounds(self.actor)[0]["state"], "complete")

    def test_off_during_post_retains_pending_intent_and_settles_a_quiet_follow(self):
        self.coordinator.notify(self.actor, "build", "threads")

        def off_before_response(op_id):
            self.coordinator.notify(self.actor, "build", "off")
            self.assertEqual(self.ledger.pending_auto_follows(self.actor)[0]["op_id"], op_id)

        self.backend.before_mutation = off_before_response
        result = self.coordinator.send(self.actor, "build", "Sent while switching off")
        self.assertEqual(self.coordinator.following(self.actor)[0]["root_id"], result["post"]["id"])
        self.assertEqual(self.ledger.notification_mode(self.actor, "build")["mode"], "off")

    def test_known_rejection_discards_intent_and_invalid_canonical_result_stays_uncertain(self):
        self.backend.reject_post = True
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "Rejected")
        self.assertEqual(self.ledger.pending_auto_follows(self.actor), [])
        self.backend.reject_post = False
        self.backend.return_wrong_post = True
        with self.assertRaises(BackendError) as error:
            self.coordinator.send(self.actor, "build", "Malformed reply")
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(self.ledger.pending_auto_follows(self.actor)), 1)
        self.assertEqual(self.coordinator.following(self.actor), [])

    def test_file_only_send_journals_follow_before_upload(self):
        self.backend = AttachmentBackend()
        self.coordinator = Coordinator(self.ledger, self.backend)
        source = Path(self.home.name) / "image.png"
        source.write_bytes(b"image")

        def pending_before_upload(_op_id):
            self.assertEqual(len(self.ledger.pending_auto_follows(self.actor)), 1)
            self.assertEqual(self.coordinator.following(self.actor), [])

        self.backend.before_upload = pending_before_upload
        result = self.coordinator.send(self.actor, "build", "", files=[source])
        self.assertEqual(self.coordinator.following(self.actor)[0]["root_id"], result["post"]["id"])

    def test_cli_and_diagnostics_expose_mode_follow_state_and_manual_stays_quiet(self):
        self.backend.add("root")
        self.run_cli("notify", "build", "threads")
        self.run_cli("follow", "root")
        self.backend.add("new-reply", root="root")
        self.assertEqual(self.run_cli("following", "build")[0]["root_id"], "root")
        inbox = self.run_cli("inbox")[0]
        self.assertEqual((inbox["notification_mode"], inbox["followed_thread_count"]), ("threads", 1))
        page = self.run_cli("threads", "build")
        self.assertEqual(page["notification_mode"], "threads")
        self.assertTrue(page["threads"][0]["following"])
        self.assertEqual(self.run_cli("doctor")["notifications"]["following_count"], 1)
        manual = NativeIdentity("codex", "33333333-3333-4333-8333-333333333333", parent_known=False)
        self.coordinator.register(manual, "Manual", mode="manual")
        self.coordinator.notify(manual.key, "build", "threads")
        self.coordinator.follow(manual.key, "root")
        self.backend.add("manual-arrival", root="root")
        self.coordinator.sync(manual.key)
        self.assertEqual(self.ledger.notification_candidates(manual.key), [])
        self.assertFalse(self.ledger.notification_mode(manual.key, "build")["active"])

    def test_reference_cannot_follow_and_offline_unknown_reply_is_never_guessed(self):
        self.backend.chats.append({"id": "reference", "name": "Reference", "kind": "channel", "role": "observer"})
        self.backend.add("reference-root", channel="reference")
        self.coordinator.sync(self.actor)
        with self.assertRaisesRegex(LedgerError, "full participation"):
            self.coordinator.follow(self.actor, "reference-root")
        with patch.object(self.backend, "request", side_effect=AssertionError("Dormant preference stays local")):
            value = self.coordinator.notify(self.actor, "reference", "threads")
        self.assertFalse(value["active"])
        with patch.object(self.backend, "post", side_effect=BackendError("Offline", status=503)), \
                self.assertRaisesRegex(LedgerError, "canonical root from following"):
            self.coordinator.unfollow(self.actor, "unknown-reply")

    def test_confirmed_join_allows_immediate_send_and_active_notify_without_daemon_feed(self):
        child = self.inherited_child()
        self.assertEqual(self.ledger.conversations(child.key)[0]["role"], "observer")
        calls = len(self.backend.change_calls)
        self.coordinator.mutate(child.key, "join", channel_id="build")
        self.assertEqual(len(self.backend.change_calls), calls)
        value = self.coordinator.notify(child.key, "build", "threads")
        self.assertTrue(value["active"])
        original_mutate = self.backend.mutate

        def own_post(action, actor, op_id, **payload):
            result = original_mutate(action, actor, op_id, **payload)
            result["post"]["user_id"] = "user-" + actor
            return result

        with patch.object(self.backend, "mutate", side_effect=own_post):
            result = self.coordinator.send(child.key, "build", "Immediate joined reply")
        self.assertEqual(result["post"]["user_id"], "user-" + child.key)
        self.assertEqual(self.coordinator.following(child.key)[0]["root_id"], result["post"]["id"])

    def test_join_refresh_failure_warns_and_preserves_canonical_completion(self):
        child = self.inherited_child()
        with patch.object(self.backend, "conversations", side_effect=BackendError("Snapshot unavailable")):
            result = self.coordinator.mutate(child.key, "join", channel_id="build")
        self.assertIn("Join completed", result["sync_warning"])
        self.assertEqual(self.ledger.outbounds(child.key)[0]["state"], "complete")
        self.assertEqual(self.ledger.db.execute(
            "SELECT surface FROM diagnostics WHERE actor=?", (child.key,)).fetchone()[0], "join_access_refresh")
        self.coordinator.sync(child.key)
        self.assertTrue(self.coordinator.notify(child.key, "build", "threads")["active"])
        self.assertEqual(len(self.backend.mutation_calls), 1)

    def test_canonical_join_recovery_refreshes_access_without_feed_or_repost(self):
        child = self.inherited_child()
        self.backend.lost_join = True
        with self.assertRaises(BackendError):
            self.coordinator.mutate(child.key, "join", channel_id="build")
        self.assertEqual(next(c for c in self.ledger.conversations(child.key) if c["id"] == "build")["role"], "observer")
        calls = len(self.backend.change_calls)
        self.coordinator.reconcile_outbound(child.key)
        self.assertEqual(len(self.backend.change_calls), calls)
        self.assertTrue(self.coordinator.notify(child.key, "build", "threads")["active"])
        self.assertEqual(len(self.backend.mutation_calls), 1)

    def test_send_refreshes_stale_observer_metadata_after_external_confirmed_join(self):
        self.backend.chats[0]["role"] = "observer"
        self.coordinator.sync(self.actor)
        self.backend.chats[0]["role"] = "member"
        result = self.coordinator.send(self.actor, "build", "Server membership is current")
        self.assertEqual(result["post"]["message"], "Server membership is current")

    def test_missing_boundary_capability_fails_before_post_or_preference_change(self):
        self.backend.capability = None
        before = tuple(self.ledger.db.iterdump())
        with self.assertRaisesRegex(BackendError, "attention-boundary"):
            self.coordinator.notify(self.actor, "build", "threads")
        with self.assertRaisesRegex(BackendError, "attention-boundary"):
            self.coordinator.send(self.actor, "build", "Not posted")
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)
        self.assertEqual(self.backend.mutation_calls, [])


class AttentionBoundaryWireTests(unittest.TestCase):
    def test_boundary_validates_exact_conversation_and_nonnegative_integer_sequence(self):
        backend = Backend("http://fixture.invalid", "fixture")
        calls = []
        backend.request = lambda *args, **kwargs: calls.append((args, kwargs)) or {"conversation_id": "room", "after_seq": 3}
        self.assertEqual(backend.attention_boundary("actor", "room")["after_seq"], 3)
        self.assertEqual(calls, [(("GET", "/conversations/room/attention-boundary"), {"actor": "actor"})])
        for result in ({"conversation_id": "other", "after_seq": 3}, {"conversation_id": "room", "after_seq": True},
                       {"conversation_id": "room", "after_seq": -1}, {"conversation_id": "room"}):
            backend.request = lambda *_args, **_kwargs: result
            with self.subTest(result=result), self.assertRaises(BackendError):
                backend.attention_boundary("actor", "room")
