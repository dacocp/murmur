"""A pending import is an explicit route gate, not apparent delivery failure."""

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from murmur import diagnostics, hooks
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger
from murmur.native import NativeIdentity


class OnboardingDiagnosticsTests(unittest.TestCase):
    def test_pending_import_error_and_resume_are_visible_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            self.addCleanup(ledger.close)
            hooks.initialize(ledger.db)
            identity = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
            ledger.enroll(identity.key, asdict(identity), "Test session", user_id="fixture",
                          onboarding=("pending", "codex:parent"))
            plan = ledger.onboarding(identity.key)
            ledger.finish_onboarding(identity.key, plan["plan_id"], "Saved-copy outcome is uncertain")
            backend = Mock()
            backend.request.return_value = {"version": "fixture"}
            coordinator = Coordinator(ledger, backend)
            before = tuple(ledger.db.iterdump())
            with patch.object(diagnostics.native, "verify_target", return_value=identity), \
                    patch.object(diagnostics, "route_capabilities", return_value={
                        "idle_queue": {"status": "supported"}, "busy_context": {"status": "supported"}}):
                result = diagnostics.report(coordinator, identity,
                                            {"delivery_enabled": True, "busy_context_enabled": True})
            self.assertEqual(result["onboarding"]["status"], "pending")
            self.assertEqual(result["onboarding"]["error"], "Saved-copy outcome is uncertain")
            self.assertEqual(result["onboarding"]["plan_id"], plan["plan_id"])
            self.assertEqual(result["onboarding"]["progress"], {})
            self.assertIn("Repeat register", result["onboarding"]["resume"])
            for route in result["routes"].values():
                self.assertIn("pending onboarding", route["blocked_by"])
            self.assertEqual(tuple(ledger.db.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
