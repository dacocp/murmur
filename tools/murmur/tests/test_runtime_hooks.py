"""Native hook policy with verified native facts and the real operational ledger."""

from pathlib import Path
from dataclasses import replace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import hooks
from murmur.coordinator import actor_key, identity_dict, routing_binding
from murmur.ledger import Ledger
from murmur.native import HookEvent, NativeError, NativeIdentity, parse_wake
from murmur.native.lineage import observation


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "33333333-3333-4333-8333-333333333333"
OLD_TURN = "codex:22222222-2222-4222-8222-222222222222"
NEW_TURN = "codex:44444444-4444-4444-8444-444444444444"
THIRD_TURN = "codex:66666666-6666-4666-8666-666666666666"


class RuntimeHookTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.home.name) / "ledger.sqlite3", clock=lambda: 100)
        self.identity = NativeIdentity("codex", SESSION, parent_known=True)
        self.actor = actor_key(self.identity)
        self.ledger.enroll(self.actor, identity_dict(self.identity), "Alex", "self")
        self.ledger.sync_conversations(self.actor, [{"id": "build", "name": "Build review",
                                                   "kind": "channel", "role": "member"}])
        self.ledger.ingest(self.actor, [{"id": "request", "channel_id": "build", "user_id": "peer",
                                       "author": "Bea", "root_id": "", "create_at": 101,
                                       "update_at": 101, "causal": "exchange"}], 1, 1)

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def handle(self, event, *, identity=None, turn=OLD_TURN, token=None, creation=None, source=None):
        native_event = {"submit": "UserPromptSubmit", "complete": "Stop", "interrupt": "Interrupt",
                        "session_start": "SessionStart", "session_end": "SessionEnd",
                        "post_tool": "PostToolUse"}[event]
        prompt = "You have new messages. [wake:" + token + "]" if token else "Ordinary work"
        captured = HookEvent(identity or self.identity, event, native_event, turn,
                             prompt=prompt, wake_token=token, creation=creation, source=source)
        with patch("murmur.native.parse_hook", return_value=captured):
            return hooks.handle(self.ledger, captured.identity.provider, native_event, {"prompt": prompt})

    def attempt(self, token="wake"):
        return self.ledger.create_attempt(self.actor, routing_binding(self.identity), token)

    def test_compaction_reminder_is_local_nonconsuming_and_independent_of_busy_delivery(self):
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, False, delivery_enabled=True)
        self.assertFalse(hooks.busy_context_enabled(self.ledger, self.actor))
        before = tuple(self.ledger.db.iterdump())
        result = self.handle("session_start", source="compact")
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("Murmur inbox", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)
        for source in (None, "startup", "resume", "fork"):
            with self.subTest(source=source):
                self.assertEqual(self.handle("session_start", source=source), {})

    def test_compaction_reminder_requires_current_delivery_lease_and_enrolled_route(self):
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, True, delivery_enabled=False)
        self.assertEqual(self.handle("session_start", source="compact"), {})
        hooks.publish_control(self.ledger, False, delivery_enabled=True)
        replacement = replace(self.identity, target={"native_epoch": "unregistered-owner"})
        self.assertEqual(self.handle("session_start", identity=replacement, source="compact"), {})
        self.ledger.clock = lambda: 100 + hooks.CONTROL_LEASE_MS + 1
        self.assertEqual(self.handle("session_start", source="compact"), {})
        self.ledger.clock = lambda: 99
        self.assertEqual(self.handle("session_start", source="compact"), {})

    def test_compaction_does_not_give_unenrolled_children_or_manual_sessions_context(self):
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, True)
        child = NativeIdentity("codex", OTHER, parent_session_id=SESSION, parent_known=True)
        self.assertEqual(self.handle("session_start", identity=child, source="compact"), {})
        self.assertEqual([a["actor"] for a in self.ledger.actors()], [self.actor])
        self.ledger.enroll(child.actor, identity_dict(child), "Manual child", "other-user", mode="manual")
        with patch.object(self.ledger, "has_pending_attention", side_effect=AssertionError("Manual stays quiet")):
            self.assertEqual(self.handle("session_start", identity=child, source="compact"), {})

    def test_compaction_does_not_remind_for_muted_observer_or_read_messages(self):
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, True)
        self.ledger.set_mute(self.actor, "build", True)
        self.assertEqual(self.handle("session_start", source="compact"), {})
        self.ledger.set_mute(self.actor, "build", False)
        self.ledger.ingest(self.actor, [{"id": "request-2", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 102, "update_at": 102, "causal": "exchange"}], 2, 1)
        self.assertIn("hookSpecificOutput", self.handle("session_start", source="compact"))
        self.ledger.sync_conversations(self.actor, [{"id": "build", "name": "Build review",
                                                   "kind": "channel", "role": "observer"}])
        self.assertEqual(self.handle("session_start", source="compact"), {})
        self.ledger.sync_conversations(self.actor, [{"id": "build", "name": "Build review",
                                                   "kind": "channel", "role": "member"}])
        self.ledger.ingest(self.actor, [{"id": "request-3", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 103, "update_at": 103, "causal": "exchange"}], 3, 1)
        self.assertIn("hookSpecificOutput", self.handle("session_start", source="compact"))
        self.ledger.mark_read(self.actor, ["request", "request-2", "request-3"])
        self.assertEqual(self.handle("session_start", source="compact"), {})

    def test_claude_enrolled_session_receives_the_same_compaction_cue(self):
        identity = NativeIdentity("claude", SESSION, parent_known=True)
        self.ledger.enroll(identity.actor, identity_dict(identity), "Claude", "claude-user")
        self.ledger.sync_conversations(identity.actor, [{"id": "build", "name": "Build review",
                                                       "kind": "channel", "role": "member"}])
        self.ledger.ingest(identity.actor, [{"id": "request", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 101, "update_at": 101, "causal": "exchange"}], 1, 1)
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, False, delivery_enabled=True)
        response = self.handle("session_start", identity=identity, source="compact")
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIsNone(self.ledger.unread_posts(identity.actor)[0]["read_at"])

    def test_busy_hook_is_inert_without_a_current_coordinator_lease(self):
        hooks.initialize(self.ledger.db)
        with patch("murmur.native.parse_hook", side_effect=AssertionError("Disabled hook must stay inert")):
            self.assertEqual(hooks.handle(self.ledger, "codex", "PostToolUse", {}), {})
            hooks.publish_control(self.ledger, True)
            self.ledger.clock = lambda: 100 + hooks.CONTROL_LEASE_MS + 1
            self.assertEqual(hooks.handle(self.ledger, "codex", "PostToolUse", {}), {})
            self.ledger.clock = lambda: 99  # Clock reversal cannot extend a lease.
            self.assertEqual(hooks.handle(self.ledger, "codex", "PostToolUse", {}), {})
        self.assertEqual(self.ledger.attempts(), [])
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_busy_watermark_is_complete_before_real_ledger_claim_mints_the_nonce(self):
        self.handle("submit")
        hooks.publish_control(self.ledger, True)
        original_turn = self.ledger.active_turn(self.actor)
        path = Path(self.home.name) / "synthetic-frontier.jsonl"
        path.write_bytes(b'{"synthetic":"complete pre-notification frontier"}\n')
        captured = HookEvent(self.identity, "post_tool", "PostToolUse", OLD_TURN,
                             tool_id="tool-after-frontier", transcript_path=str(path))
        request = {"schema": 2, "kind": "codex-canonical-event-jsonl", "offset": path.stat().st_size}
        sequence = []
        nonce = "post-capture-" + "n" * 20

        def capture(event):
            self.assertEqual(event, captured)
            claim.assert_not_called()
            mint.assert_not_called()
            self.assertEqual(self.ledger.attempts(self.actor), [])
            self.assertEqual(path.stat().st_size, request["offset"])
            sequence.append("frontier captured")
            return request

        def mint_after_capture(size):
            self.assertEqual(size, 24)
            self.assertEqual(sequence, ["frontier captured"])
            self.assertEqual(path.stat().st_size, request["offset"])
            self.assertNotIn(nonce.encode(), path.read_bytes())
            sequence.append("nonce minted")
            return nonce

        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", side_effect=capture), \
                patch.object(self.ledger, "claim_context", wraps=self.ledger.claim_context) as claim, \
                patch("murmur.ledger.secrets.token_urlsafe", side_effect=mint_after_capture) as mint:
            result = hooks.handle(self.ledger, "codex", "PostToolUse", {})
        self.assertEqual(sequence, ["frontier captured", "nonce minted"])
        claim.assert_called_once()
        mint.assert_called_once_with(24)
        self.assertEqual(parse_wake(result["hookSpecificOutput"]["additionalContext"]), nonce)
        attempt = self.ledger.attempt(nonce)
        self.assertEqual((attempt["receipt_request"], attempt["post_ids"], attempt["status"]),
                         (request, ["request"], "pending"))
        self.assertEqual(self.ledger.active_turn(self.actor), original_turn)
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_failed_busy_watermark_never_claims_a_nonce_or_emits_context(self):
        self.handle("submit")
        hooks.publish_control(self.ledger, True)
        original_turn = self.ledger.active_turn(self.actor)
        original_receipts = self.ledger._unread(self.actor)
        captured = HookEvent(self.identity, "post_tool", "PostToolUse", OLD_TURN,
                             tool_id="incomplete-frontier", transcript_path="/synthetic/history.jsonl")
        payload = {"ordinary_tool_result": "original output remains available"}
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", side_effect=NativeError("Incomplete boundary")), \
                patch.object(self.ledger, "claim_context", wraps=self.ledger.claim_context) as claim, \
                patch("murmur.ledger.secrets.token_urlsafe") as mint:
            result = hooks.handle(self.ledger, "codex", "PostToolUse", payload)
        self.assertEqual(result, {})
        claim.assert_not_called()
        mint.assert_not_called()
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertEqual(self.ledger._unread(self.actor), original_receipts)
        self.assertEqual(self.ledger.active_turn(self.actor), original_turn)
        self.assertEqual(payload, {"ordinary_tool_result": "original output remains available"})

    def test_native_client_evidence_updates_mid_turn_without_rewriting_frozen_work(self):
        old = identity_dict(self.identity)
        attempt = self.ledger.create_attempt(self.actor, routing_binding(self.identity), "frozen",
                                             expected_identity=old, refreshed_identity=old)
        self.ledger.set_transport(attempt["token"], "accepted")
        frozen = self.ledger.attempt(attempt["token"])
        self.handle("submit")
        active = self.ledger.active_turn(self.actor)
        self.ledger.db.execute("UPDATE receipts SET read_at=100 WHERE actor=?", (self.actor,))
        hooks.publish_control(self.ledger, True)
        desktop = replace(self.identity, parent_known=False,
                          evidence={"desktop_wake": {"schema": 1, "owner": "fixture"}})
        self.assertEqual(self.handle("post_tool", identity=desktop), {})
        self.assertEqual(self.ledger.actor(self.actor)["identity"]["evidence"], desktop.evidence)
        self.assertEqual(self.ledger.active_turn(self.actor), active)
        self.assertEqual(self.ledger.attempt("frozen"), frozen)
        cli = replace(self.identity, parent_known=False, evidence={"desktop_wake": None})
        self.handle("complete", identity=cli)
        self.assertEqual(self.ledger.actor(self.actor)["identity"]["evidence"], {"desktop_wake": None})
        self.assertEqual(self.ledger.attempt("frozen"), frozen)

    def test_evidence_cas_skips_changed_route_and_normal_submit_still_runs(self):
        old = identity_dict(self.identity)
        desktop = replace(self.identity, evidence={"desktop_wake": {"schema": 1}})
        changed = replace(self.identity, target={"cwd": "/new-route"},
                          evidence={"desktop_wake": None})
        self.ledger.enroll(self.actor, identity_dict(changed), "Alex", "self")
        self.assertFalse(self.ledger.refresh_native_evidence(self.actor, old, identity_dict(desktop)))
        self.assertEqual(self.ledger.actor(self.actor)["identity"], identity_dict(changed))
        self.handle("submit", identity=desktop, turn=NEW_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        self.assertEqual(self.ledger.actor(self.actor)["identity"], identity_dict(desktop))

    def test_coordinator_revocation_survives_restart_without_reading_credentials(self):
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, True)
        self.assertTrue(hooks.busy_context_enabled(self.ledger))
        hooks.publish_control(self.ledger, False)
        path = self.ledger.path
        self.ledger.close()
        self.ledger = Ledger(path, clock=lambda: 100)
        with patch("murmur.native.parse_hook", side_effect=AssertionError("Revoked hook must stay inert")):
            self.assertEqual(hooks.handle(self.ledger, "codex", "PostToolUse", {}), {})
        self.assertEqual(self.ledger.attempts(), [])

    def test_existing_control_row_migrates_without_changing_its_enabled_state(self):
        self.ledger.db.execute("""CREATE TABLE runtime_control(
            singleton INTEGER PRIMARY KEY,busy_context_enabled INTEGER,refreshed_at INTEGER)""")
        self.ledger.db.execute("INSERT INTO runtime_control VALUES(1,1,100)")
        hooks.initialize(self.ledger.db)
        self.assertTrue(hooks.busy_context_enabled(self.ledger, self.actor))
        self.assertFalse(hooks.notifications_enabled(self.ledger))
        hooks.publish_control(self.ledger, True, [self.actor])
        self.assertTrue(hooks.busy_context_enabled(self.ledger, self.actor))
        self.assertTrue(hooks.notifications_enabled(self.ledger))
        self.assertFalse(hooks.busy_context_enabled(self.ledger, "codex:" + OTHER))

    def test_unenrolled_session_start_is_passive_and_wake_is_held(self):
        other = NativeIdentity("codex", OTHER, parent_known=True)
        creation = observation({"hook_event_name": "SessionStart", "session_id": OTHER, "source": "startup"},
                               {"pid": 2300, "proc_start": "fixture birth", "entrypoint": "cli",
                                "version": "2.1.310", "root_start_verified": True})
        self.assertEqual(self.handle("session_start", identity=other, creation=creation), {})
        self.assertEqual([a["actor"] for a in self.ledger.actors()], [self.actor])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations").fetchone()[0], 1)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_proofs").fetchone()[0], 0)
        self.attempt()
        result = self.handle("submit", identity=other, token="wake")
        self.assertEqual(result["decision"], "block")
        self.assertIn("not explicitly enrolled", result["reason"])
        self.assertEqual(self.ledger.attempt("wake")["status"], "pending")
        self.assertIsNone(self.ledger.active_turn(actor_key(other)))

    def test_wrong_recipient_cannot_admit_another_sessions_notification(self):
        other = NativeIdentity("codex", OTHER, parent_known=True)
        self.ledger.enroll(actor_key(other), identity_dict(other), "Other", "other-user")
        self.attempt()
        result = self.handle("submit", identity=other, token="wake")
        self.assertEqual(result["decision"], "block")
        self.assertIn("recipient", result["reason"])
        self.assertEqual(self.ledger.attempt("wake")["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_retired_delayed_token_and_changed_native_epoch_do_not_admit(self):
        self.attempt("retired")
        self.ledger.retire_attempt("retired", "Old delivery retired")
        result = self.handle("submit", token="retired")
        self.assertEqual(result["decision"], "block")
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.ledger.ingest(self.actor, [{"id": "later", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 102, "update_at": 102, "causal": "exchange"}], 2, 1)
        self.attempt("current")
        changed = NativeIdentity("codex", SESSION, parent_known=True, target={"native_epoch": "replaced"})
        result = self.handle("submit", identity=changed, token="current")
        self.assertEqual(result["decision"], "block")
        self.assertIn("epoch", result["reason"])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_new_verified_submit_supersedes_missing_stop_without_losing_causality(self):
        self.attempt()
        self.assertEqual(self.handle("submit", token="wake"), {})
        self.assertEqual(self.handle("submit", turn=NEW_TURN), {})
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        self.assertEqual(self.ledger.attempt("wake")["status"], "interrupted")
        self.assertEqual(self.ledger.causal_context(self.actor), {"chain_ids": ["exchange"]})
        self.handle("complete", turn=OLD_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        self.handle("complete", turn=NEW_TURN)
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertEqual(self.ledger.causal_context(self.actor), {"chain_ids": ["exchange"]})

    def test_duplicate_old_submit_does_not_interrupt_the_new_active_turn(self):
        self.attempt()
        self.handle("submit", token="wake")
        self.handle("submit", turn=NEW_TURN)
        path = self.ledger.path
        self.ledger.close()
        self.ledger = Ledger(path, clock=lambda: 100)
        self.handle("submit", token="wake", turn=OLD_TURN)
        active = self.ledger.active_turn(self.actor)
        self.assertIsNotNone(active)
        self.assertEqual(active["turn_key"], NEW_TURN)

    def test_rejected_delayed_notification_preserves_the_current_active_turn(self):
        self.attempt("retired")
        self.ledger.retire_attempt("retired", "Previous native attempt retired")
        self.ledger.ingest(self.actor, [{"id": "later", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 102, "update_at": 102, "causal": "exchange"}], 2, 1)
        self.attempt("pending")
        self.handle("submit", turn=NEW_TURN)
        result = self.handle("submit", token="retired", turn=THIRD_TURN)
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        changed = NativeIdentity("codex", SESSION, parent_known=True, target={"native_epoch": "replaced"})
        result = self.handle("submit", identity=changed, token="pending", turn=THIRD_TURN)
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        self.assertEqual(self.ledger.attempt("pending")["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_ordinary_old_submit_and_unproved_cross_epoch_replacement_preserve_new_turn(self):
        self.handle("submit", turn=OLD_TURN)
        self.handle("submit", turn=NEW_TURN)
        self.handle("submit", turn=OLD_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        changed = NativeIdentity("codex", SESSION, parent_known=True, target={"native_epoch": "replaced"})
        self.handle("submit", identity=changed, turn=THIRD_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)

    def test_valid_notification_can_replace_same_epoch_turn_after_a_missing_stop(self):
        self.attempt()
        self.handle("submit", turn=OLD_TURN)
        self.assertEqual(self.handle("submit", turn=NEW_TURN, token="wake"), {})
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        self.assertEqual(self.ledger.attempt("wake")["status"], "admitted")
        self.handle("complete", turn=OLD_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)

    def test_session_lifecycle_is_not_completion_evidence_and_ancestry_is_preserved(self):
        registered = NativeIdentity("codex", SESSION, parent_known=True, evidence={"parent_proof": "verified"})
        self.ledger.update_identity(self.actor, identity_dict(registered))
        fresh = NativeIdentity("codex", SESSION, evidence={"route": "fresh"})
        self.handle("submit", identity=fresh)
        value = self.ledger.actor(self.actor)["identity"]
        self.assertTrue(value["parent_known"])
        self.assertIsNone(value["parent_session_id"])
        self.assertEqual(value["evidence"], {"parent_proof": "verified", "route": "fresh"})
        self.handle("session_start")
        self.handle("session_end")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], OLD_TURN)


if __name__ == "__main__":
    unittest.main()
