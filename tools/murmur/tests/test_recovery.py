"""Recovery uses frozen owner facts and fresh routing snapshots, never guessed exit."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import config, daemon, hooks
from murmur.coordinator import identity_dict, routing_binding
from murmur.ledger import Ledger
from murmur.native import DeliveryResult, HookEvent, NativeIdentity


HOST = "local_11111111-1111-4111-8111-111111111111"
OLD_ENGINE = "22222222-2222-4222-8222-222222222222"
NEW_ENGINE = "33333333-3333-4333-8333-333333333333"
OLD_TURN = "claude:" + OLD_ENGINE + ":old-prompt"
NEW_TURN = "claude:" + NEW_ENGINE + ":new-prompt"


class SnapshotBackend:
    def __init__(self, inject=None):
        self.inject = inject
        self.injected = False

    def conversations(self, actor):
        return [{"id": "build", "name": "Build review", "kind": "channel", "role": "member"}]

    def changes(self, actor, cursor, generation):
        if self.inject and not self.injected:
            self.injected = True
            self.inject()
        post = {"id": "request", "channel_id": "build", "user_id": "peer", "author": "Bea",
                "message": "Current question", "root_id": "", "create_at": 101, "update_at": 101}
        return {"attention_version": 3, "conversations": self.conversations(actor),
                "changes": [{"seq": 1, "first_seen_seq": 1, "post": post, "notification_eligible": True}] if cursor == 0 else [],
                "cursor": 1, "generation": 1, "more": False, "reset": generation != 1}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.root = Path(self.home.name)
        self.path = config.ledger_path(self.root)
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.old = NativeIdentity("claude", HOST, parent_known=True,
                                  target={"pid": 41, "engine_id": OLD_ENGINE, "proc_start": "old-birth",
                                          "native_epoch": "old-epoch"})
        self.fresh = NativeIdentity("claude", HOST, parent_known=True,
                                    target={"pid": 42, "engine_id": NEW_ENGINE, "proc_start": "new-birth",
                                            "native_epoch": "new-epoch"})
        self.actor = self.old.actor
        self.ledger.enroll(self.actor, identity_dict(self.old), "Alex", "self")
        self.ledger.sync_conversations(self.actor, [{"id": "build", "name": "Build review",
                                                   "kind": "channel", "role": "member"}])
        config.save({"url": "http://fake", "token": "fake", "delivery_enabled": True}, self.root)

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def handle(self, ledger, identity, event, turn, token=None):
        native_event = "UserPromptSubmit" if event == "submit" else "Stop"
        prompt = "You have new messages. [wake:" + token + "]" if token else "Continue requested work"
        captured = HookEvent(identity, event, native_event, turn, prompt=prompt, wake_token=token)
        with patch("murmur.native.parse_hook", return_value=captured):
            return hooks.handle(ledger, "claude", native_event, {"prompt": prompt})

    def start_old(self):
        self.ledger.start_turn(self.actor, routing_binding(self.old), OLD_TURN,
                               native_identity=identity_dict(self.old))

    def test_positive_exit_uses_frozen_old_owner_then_late_stop_cannot_clear_new_turn(self):
        self.start_old()
        self.ledger.update_identity(self.actor, identity_dict(self.fresh))
        with patch("murmur.native.owner_process_state", return_value={"status": "ended", "reason": "ESRCH"}) as owner:
            self.assertEqual(self.handle(self.ledger, self.fresh, "submit", NEW_TURN), {})
        owner.assert_called_once_with(self.old, timeout=0.2)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["identity"], identity_dict(self.fresh))
        self.handle(self.ledger, self.old, "complete", OLD_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEW_TURN)

    def test_alive_or_unknown_owner_cannot_be_replaced_by_a_new_epoch(self):
        self.start_old()
        for status in ("alive", "unknown"):
            with self.subTest(status=status), patch("murmur.native.owner_process_state", return_value={"status": status}) as owner:
                self.assertEqual(self.handle(self.ledger, self.fresh, "submit", NEW_TURN), {})
            owner.assert_called_once_with(self.old, timeout=0.2)
            self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], OLD_TURN)
            self.assertEqual(self.ledger.actor(self.actor)["identity"], identity_dict(self.old))

    def test_stale_positive_exit_result_cannot_interrupt_a_newer_tuple(self):
        self.start_old()
        newest = NativeIdentity("claude", HOST, parent_known=True,
                               target={"pid": 43, "native_epoch": "newest-epoch"})
        newest_turn = "claude:44444444-4444-4444-8444-444444444444:newest-prompt"

        def owner_exited(identity, timeout):
            concurrent = Ledger(self.path)
            try:
                self.assertTrue(concurrent.interrupt(self.actor, OLD_TURN, routing_binding(self.old)))
                concurrent.start_turn(self.actor, routing_binding(newest), newest_turn,
                                      native_identity=identity_dict(newest))
                concurrent.update_identity(self.actor, identity_dict(newest))
            finally:
                concurrent.close()
            return {"status": "ended", "reason": "Old owner is gone"}

        with patch("murmur.native.owner_process_state", side_effect=owner_exited):
            self.handle(self.ledger, self.fresh, "submit", NEW_TURN)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], newest_turn)
        self.assertEqual(self.ledger.actor(self.actor)["identity"], identity_dict(newest))

    def test_automatic_input_never_uses_process_exit_to_bypass_cross_epoch_hold(self):
        post = {"id": "request", "channel_id": "build", "user_id": "peer", "author": "Bea",
                "root_id": "", "create_at": 101}
        self.ledger.ingest(self.actor, [post], 1, 1)
        self.ledger.create_attempt(self.actor, routing_binding(self.fresh), "wake")
        self.start_old()
        with patch("murmur.native.owner_process_state", return_value={"status": "ended"}) as owner:
            result = self.handle(self.ledger, self.fresh, "submit", NEW_TURN, token="wake")
        owner.assert_not_called()
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], OLD_TURN)
        self.assertEqual(self.ledger.attempt("wake")["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def fresh_hook_during_http(self):
        concurrent = Ledger(self.path)
        try:
            self.handle(concurrent, self.fresh, "submit", NEW_TURN)
            self.handle(concurrent, self.fresh, "complete", NEW_TURN)
        finally:
            concurrent.close()

    def test_daemon_refreshes_identity_after_network_sync_without_overwriting_fresh_hook(self):
        backend = SnapshotBackend(self.fresh_hook_during_http)
        with patch("murmur.control.Backend", return_value=backend), \
             patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity) as verify, \
             patch("murmur.native.deliver", return_value=DeliveryResult("accepted")) as deliver, \
             patch("murmur.native.owner_process_state", return_value={"status": "unknown"}):
            daemon.run(self.root, once=True)
        verify.assert_called_once_with(self.fresh)
        self.assertEqual(deliver.call_count, 1)
        self.assertEqual(deliver.call_args.args[0], self.fresh)
        self.assertEqual(self.ledger.actor(self.actor)["identity"], identity_dict(self.fresh))

    def test_target_changed_during_verification_is_held_by_attempt_compare_and_set(self):
        def verify_stale(identity):
            self.assertEqual(identity, self.old)
            self.fresh_hook_during_http()
            return identity

        backend = SnapshotBackend()
        with patch("murmur.control.Backend", return_value=backend), \
             patch("murmur.native.prepare_idle_target", side_effect=verify_stale), \
             patch("murmur.native.deliver", return_value=DeliveryResult("accepted")) as deliver, \
             patch("murmur.native.owner_process_state", return_value={"status": "unknown"}):
            daemon.run(self.root, once=True)
        deliver.assert_not_called()
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertEqual(self.ledger.actor(self.actor)["identity"], identity_dict(self.fresh))
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 1)
        with patch("murmur.control.Backend", return_value=backend), \
             patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity), \
             patch("murmur.native.deliver", return_value=DeliveryResult("accepted")) as deliver, \
             patch("murmur.native.owner_process_state", return_value={"status": "unknown"}):
            daemon.run(self.root, once=True)
        self.assertEqual(deliver.call_count, 1)
        self.assertEqual(deliver.call_args.args[0], self.fresh)


if __name__ == "__main__":
    unittest.main()
