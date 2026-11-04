"""Manual sidechats use their own identity without native history or wakeups."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli
from murmur import config, daemon, hooks, native
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger
from test_coordinator import FakeBackend


SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "codex:22222222-2222-4222-8222-222222222222"


class SidechatRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = config.ledger_path(self.root)
        self.identity = native.NativeIdentity("codex", SESSION)
        self.actor = self.identity.actor
        self.backend = FakeBackend()
        config.save({"url": "http://fixture.invalid", "token": "fixture",
                     "delivery_enabled": True, "busy_context_enabled": True}, self.root)

    def command(self, *arguments):
        output = io.StringIO()
        with patch.dict(os.environ, {"MURMUR_HOME": str(self.root)}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch.object(native, "current_identity", return_value=self.identity), \
                redirect_stdout(output):
            cli.main(list(arguments))
        return json.loads(output.getvalue())

    def enroll(self):
        with patch.object(native, "resolve_identity", side_effect=AssertionError("No stored sidechat metadata")):
            return self.command("register", "--sidechat")

    def test_explicit_unnamed_sidechat_registers_without_metadata_and_renames_same_account(self):
        result = self.enroll()
        self.assertEqual(result["id"], self.actor)
        self.assertEqual(result["enrollment"], {
            "mode": "manual", "automatic_wakeups": False, "inherited_access": False})
        self.assertTrue(result["name"].strip())
        with patch.object(native, "resolve_identity", side_effect=AssertionError("No stored sidechat metadata")):
            renamed = self.command("register", "--sidechat", "--name", "Review sidechat")
        self.assertEqual(renamed["user_id"], result["user_id"])
        self.assertEqual(renamed["name"], "Review sidechat")
        self.assertEqual([call[2] for call in self.backend.register_calls], [None, None])
        ledger = Ledger(self.path)
        try:
            record = ledger.actor(self.actor)
            self.assertFalse(record["identity"]["parent_known"])
            self.assertIsNone(record["identity"]["parent_session_id"])
            self.assertEqual(record["mode"], "manual")
        finally:
            ledger.close()

    def test_normal_metadata_failure_does_not_fallback_or_enroll(self):
        with patch.object(native, "resolve_identity", side_effect=native.IdentityError("No rollout")) as resolve:
            with self.assertRaisesRegex(native.IdentityError, "No rollout"):
                self.command("register", "--name", "Normal native title")
        resolve.assert_called_once()
        self.assertEqual(self.backend.register_calls, [])
        ledger = Ledger(self.path)
        try:
            self.assertEqual(ledger.actors(), [])
        finally:
            ledger.close()

    def test_normal_registration_still_requires_a_name(self):
        with self.assertRaisesRegex(ValueError, "requires --name"):
            self.command("register")
        self.assertEqual(self.backend.register_calls, [])

    def test_manual_doctor_reports_capabilities_without_native_transport_lookup(self):
        self.enroll()
        self.backend.request = lambda *_a: {"version": "fixture", "reconciliation_error": ""}
        with patch.object(native, "verify_target", side_effect=AssertionError("Manual transport")), \
                patch.object(native, "resolve_identity", side_effect=AssertionError("No stored metadata")):
            result = self.command("doctor")
        self.assertTrue(result["enrolled"])
        self.assertEqual(result["native"]["transport"], "manual")
        self.assertFalse(result["native"]["parent_known"])
        self.assertFalse(result["enrollment"]["automatic_wakeups"])
        self.assertFalse(result["busy_context"]["coordinator_lease"])
        self.assertEqual(result["activity"]["state"], "manual")

    def test_daemon_syncs_arrivals_and_uncertain_sends_without_native_work_across_restart(self):
        self.enroll()
        ledger = Ledger(self.path)
        self.backend.lose_response = True
        try:
            with self.assertRaises(RuntimeError):
                Coordinator(ledger, self.backend).mutate(self.actor, "post", channel_id="build", message="Reply")
            self.assertEqual(ledger.outbounds(self.actor)[0]["state"], "uncertain")
        finally:
            ledger.close()
        self.backend.add("later-request")
        with patch("murmur.control.Backend", return_value=self.backend), \
                patch.object(native, "verify_target", side_effect=AssertionError("Manual transport")), \
                patch.object(native, "deliver", side_effect=AssertionError("Manual transport")), \
                patch.object(native, "owner_process_state", side_effect=AssertionError("No native recovery")), \
                patch.object(native, "verify_context_receipt", side_effect=AssertionError("No native receipt")):
            daemon.run(self.root, once=True)
            daemon.run(self.root, once=True)
        ledger = Ledger(self.path)
        try:
            self.assertEqual(ledger.actor(self.actor)["mode"], "manual")
            self.assertEqual(ledger.attempts(self.actor), [])
            self.assertEqual(ledger.outbounds(self.actor)[0]["state"], "complete")
            unread = ledger.unread_posts(self.actor)
            self.assertEqual([p["id"] for p in unread], ["later-request"])
            self.assertIsNone(unread[0]["announced_at"])
        finally:
            ledger.close()

    def test_manual_hooks_do_not_capture_turns_or_emit_context_and_reject_automatic_input(self):
        self.enroll()
        ledger = Ledger(self.path, clock=lambda: 100)
        try:
            hooks.initialize(ledger.db)
            hooks.publish_control(ledger, True)
            for event, native_event in (("submit", "UserPromptSubmit"), ("post_tool", "PostToolUse"),
                                        ("complete", "Stop")):
                captured = native.HookEvent(self.identity, event, native_event, TURN,
                                           prompt="Continue", tool_id="fixture-tool")
                with patch.object(native, "parse_hook", return_value=captured), \
                        patch.object(native, "prepare_context_receipt", side_effect=AssertionError("No native receipt")):
                    self.assertEqual(hooks.handle(ledger, "codex", native_event, {"prompt": "Continue"}), {})
            prompt = "You have new messages. [wake:manual-sidechat-token]"
            captured = native.HookEvent(self.identity, "submit", "UserPromptSubmit", TURN,
                                       prompt=prompt, wake_token="manual-sidechat-token")
            with patch.object(native, "parse_hook", return_value=captured):
                result = hooks.handle(ledger, "codex", "UserPromptSubmit", {"prompt": prompt})
            self.assertEqual(result["decision"], "block")
            self.assertIn("Manual sidechat", result["reason"])
            self.assertEqual(ledger.attempts(self.actor), [])
            self.assertIsNone(ledger.active_turn(self.actor))
            self.assertFalse(ledger.actor(self.actor)["identity"]["parent_known"])
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
