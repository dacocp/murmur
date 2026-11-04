"""Explicit local recovery controls use audit records, never native impersonation."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli
from murmur.coordinator import identity_dict
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.path = Path(self.home.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        identity = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
        self.actor = identity.actor
        self.ledger.enroll(self.actor, identity_dict(identity), "Alex", "self")
        self.ledger.sync_conversations(self.actor, [{"id": "build", "name": "Build review",
                                                   "kind": "channel", "role": "member"}])
        self.ledger.ingest(self.actor, [{"id": "request", "channel_id": "build", "user_id": "peer",
                                       "author": "Bea", "root_id": "", "create_at": 101,
                                       "causal": "exchange"}], 1, 1)

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def run_control(self, action, *args, actor=None, reason="Reviewed local recovery"):
        with patch("murmur.__main__.config.ledger_path", return_value=self.path), \
             patch("murmur.__main__.config.load", side_effect=AssertionError("Recovery must not load credentials")), \
             patch("murmur.__main__.Backend", side_effect=AssertionError("Recovery must not contact server")), \
             patch("murmur.native.current_identity", side_effect=AssertionError("Recovery must not impersonate a session")), \
             redirect_stdout(io.StringIO()) as output:
            cli.main(["operator", action, actor or self.actor, "--reason", reason, *args])
        return json.loads(output.getvalue())

    def automatic_turn(self):
        self.ledger.create_attempt(self.actor, "epoch", "wake")
        self.ledger.admit("wake", self.actor, "epoch", "turn")

    def test_idle_resolve_is_audited_before_mutation_and_preserves_unread_mail(self):
        self.automatic_turn()
        self.ledger.complete(self.actor, "turn", "epoch")
        original = Ledger.resolve_causal
        audits_seen = []

        def verify_committed_audit(ledger, actor):
            connection = sqlite3.connect(self.path)
            try:
                rows = connection.execute("SELECT error FROM diagnostics WHERE actor=? AND surface='operator_requested'",
                                          (actor,)).fetchall()
                audits_seen.extend(json.loads(row[0]) for row in rows)
            finally:
                connection.close()
            return original(ledger, actor)

        with patch.object(Ledger, "resolve_causal", verify_committed_audit):
            result = self.run_control("resolve", reason="Human confirmed this exchange has ended")
        self.assertTrue(result["changed"])
        self.assertEqual(audits_seen[0]["reason"], "Human confirmed this exchange has ended")
        self.assertEqual(audits_seen[0]["control"], "resolve")
        self.assertEqual(self.ledger.causal_context(self.actor), {"chain_ids": []})
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 1)

    def test_active_resolve_is_refused_and_request_remains_audited(self):
        self.automatic_turn()
        with self.assertRaisesRegex(LedgerError, "active native turn"):
            self.run_control("resolve")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "turn")
        self.assertEqual(self.ledger.causal_context(self.actor), {"chain_ids": ["exchange"]})
        self.assertEqual(self.ledger.db.execute(
            "SELECT count(*) FROM diagnostics WHERE surface='operator_requested'").fetchone()[0], 1)

    def test_interrupt_requires_the_exact_active_epoch_and_turn(self):
        self.ledger.start_turn(self.actor, "current-epoch", "current-turn")
        for epoch, turn in (("old-epoch", "current-turn"), ("current-epoch", "old-turn")):
            result = self.run_control("interrupt", "--epoch", epoch, "--turn-key", turn)
            self.assertFalse(result["changed"])
            self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "current-turn")
        result = self.run_control("interrupt", "--epoch", "current-epoch", "--turn-key", "current-turn")
        self.assertTrue(result["changed"])
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_pending_uncertain_token_can_only_be_retired_for_its_recipient(self):
        self.ledger.create_attempt(self.actor, "epoch", "wake")
        self.ledger.set_transport("wake", "uncertain", {"queue_id": "possibly-accepted"})
        other = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", parent_known=True)
        self.ledger.enroll(other.actor, identity_dict(other), "Other", "other-user")
        self.ledger.start_turn(other.actor, "other-epoch", "other-turn")
        with self.assertRaisesRegex(ValueError, "another recipient"):
            self.run_control("retire", "--token", "wake", actor=other.actor)
        self.assertEqual(self.ledger.attempt("wake")["status"], "pending")
        self.assertEqual(self.ledger.active_turn(other.actor)["turn_key"], "other-turn")
        self.assertTrue(self.run_control("retire", "--token", "wake")["changed"])
        self.assertEqual(self.ledger.attempt("wake")["status"], "retired")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_completed_or_admitted_token_cannot_be_retired(self):
        self.automatic_turn()
        with self.assertRaises(ValueError):
            self.run_control("retire", "--token", "wake")
        self.assertEqual(self.ledger.attempt("wake")["status"], "admitted")
        self.ledger.complete(self.actor, "turn", "epoch")
        with self.assertRaises(ValueError):
            self.run_control("retire", "--token", "wake")
        self.assertEqual(self.ledger.attempt("wake")["status"], "completed")

    def test_blank_reason_refuses_recovery_without_an_audit_or_mutation(self):
        self.ledger.start_turn(self.actor, "epoch", "turn")
        with self.assertRaisesRegex(ValueError, "explicit reason"):
            self.run_control("interrupt", "--epoch", "epoch", "--turn-key", "turn", reason=" \t ")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "turn")
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM diagnostics").fetchone()[0], 0)

    def test_onboarding_skip_is_plan_scoped_audited_and_never_loads_credentials(self):
        child = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", parent_known=True,
                               parent_session_id="11111111-1111-4111-8111-111111111111")
        self.ledger.enroll(child.actor, identity_dict(child), "Child", "child-user",
                           onboarding=("pending", self.actor))
        plan = self.ledger.onboarding(child.actor)
        self.ledger.capture_pending(child.actor, plan["plan_id"])
        self.ledger.capture_saved(child.actor, plan["plan_id"], ["saved-request"], 100, 100)
        self.ledger.onboarding_step(child.actor, plan["plan_id"], "star:saved-request", "started")
        result = self.run_control("onboarding-skip", "--plan-id", plan["plan_id"], "--step", "star:saved-request",
                                  actor=child.actor, reason="Human chose not to repeat the uncertain saved write")
        self.assertEqual(result["state"], "skipped")
        self.assertEqual(self.ledger.onboarding(child.actor)["progress"], {"star:saved-request": "skipped"})
        audit = json.loads(self.ledger.db.execute("SELECT error FROM diagnostics WHERE surface='operator_requested'").fetchone()[0])
        self.assertEqual((audit["plan_id"], audit["step"]), (plan["plan_id"], "star:saved-request"))
        self.assertIn("Human chose", audit["reason"])


    def test_retire_rechecks_pending_after_native_admission_and_completion_race(self):
        self.ledger.create_attempt(self.actor, "epoch", "wake")
        original = Ledger.retire_attempt
        completed = []

        def admit_before_retirement(ledger, token, reason):
            # The CLI already read pending; native lifecycle commits before
            # retirement takes its own transaction.
            ledger.admit(token, self.actor, "epoch", "actual")
            ledger.complete(self.actor, "actual", "epoch")
            completed.append(ledger.attempt(token))
            return original(ledger, token, reason)

        with patch.object(Ledger, "retire_attempt", admit_before_retirement), self.assertRaisesRegex(
                LedgerError, "Only a pending notification"):
            self.run_control("retire", "--token", "wake")
        self.assertEqual(self.ledger.attempt("wake"), completed[0])
        self.assertEqual(self.ledger.attempt("wake")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
