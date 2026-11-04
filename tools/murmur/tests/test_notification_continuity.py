"""Notification uncertainty cannot reserve unrelated future arrivals."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from murmur import config, hooks, native
from murmur.control import network_tick
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import DeliveryResult, HookEvent, NativeIdentity, parse_wake
from test_codex_restart import RestartTransport
from test_coordinator import FakeBackend


SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "codex:22222222-2222-4222-8222-222222222222"
NEXT_TURN = "codex:33333333-3333-4333-8333-333333333333"


class NotificationContinuityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = config.ledger_path(self.root)
        self.ledger = Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())
        hooks.initialize(self.ledger.db)
        self.identity = NativeIdentity("codex", SESSION, parent_known=True,
            evidence={"desktop_wake": {"fixture": "captured owner"}})
        self.actor = self.identity.actor
        self.epoch = routing_binding(self.identity)
        self.ledger.enroll(self.actor, asdict(self.identity), "Receiver", "self")
        self.backend = FakeBackend()
        self.coordinator = Coordinator(self.ledger, self.backend)
        self.ledger.sync_conversations(self.actor, self.backend.conversations(self.actor))
        config.save(dict(url="http://fixture.invalid", token="fixture",
                         delivery_enabled=True, busy_context_enabled=True), self.root)
        hooks.publish_control(self.ledger, True)

    def add(self, post_id, *, when):
        self.backend.add(post_id, when=when)
        self.coordinator.sync(self.actor)

    def receipt(self, post_id):
        return dict(self.ledger.db.execute("SELECT * FROM receipts WHERE actor=? AND post_id=?",
                                          (self.actor, post_id)).fetchone())

    def post_tool(self, tool):
        captured = HookEvent(self.identity, "post_tool", "PostToolUse", TURN,
                             tool_id=tool, transcript_path="/fixture/rollout.jsonl")
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", return_value={"fixture": tool}):
            return hooks.handle(self.ledger, "codex", "PostToolUse", {})

    def test_late_context_proof_and_agent_receipt_preserve_independent_new_work(self):
        self.add("A", when=101)
        first = self.post_tool("tool-A")
        token_a = parse_wake(first["hookSpecificOutput"]["additionalContext"])
        sealed_a = self.ledger.attempt(token_a)
        self.assertEqual((sealed_a["status"], sealed_a["post_ids"]), ("pending", ["A"]))
        self.assertIsNotNone(sealed_a["emitted_at"])

        # Capture the exact receipts visible before an inbox-like network step.
        snapshot = self.ledger.unseen_notice_tokens(self.actor)
        self.add("B", when=102)
        second = self.post_tool("tool-B")
        token_b = parse_wake(second["hookSpecificOutput"]["additionalContext"])
        sealed_b = self.ledger.attempt(token_b)
        self.assertEqual(sealed_b["post_ids"], ["B"])
        self.assertEqual(self.post_tool("tool-C"), {})
        self.assertEqual(snapshot, [token_a])

        other = replace(self.identity, session_id="44444444-4444-4444-8444-444444444444")
        self.ledger.enroll(other.actor, asdict(other), "Other receiver", "other-self")
        before_receipts = self.receipt("A"), self.receipt("B")
        with self.assertRaisesRegex(LedgerError, "another recipient"):
            self.ledger.acknowledge_notices(other.actor, snapshot)
        self.assertEqual(self.ledger.attempt(token_a), sealed_a)

        seen = self.ledger.acknowledge_notices(self.actor, snapshot)
        self.assertFalse(seen[0]["already_seen"])
        self.assertTrue(self.ledger.acknowledge_notices(self.actor, snapshot)[0]["already_seen"])
        seen_a = self.ledger.attempt(token_a)
        self.assertIsNotNone(seen_a["agent_seen_at"])
        self.assertEqual({k: v for k, v in seen_a.items() if k != "agent_seen_at"},
                         {k: v for k, v in sealed_a.items() if k != "agent_seen_at"})
        self.assertEqual(self.ledger.attempt(token_b), sealed_b)
        self.assertEqual((self.receipt("A"), self.receipt("B")), before_receipts)

        def verify(identity, attempt, **_):
            if attempt["token"] == token_a:
                return {"status": "unknown", "reason": "Receipt still unavailable"}
            return {"status": "accepted", "evidence": {"native_record": token_b}}

        with patch("murmur.native.verify_context_receipt", side_effect=verify):
            pending = Coordinator(self.ledger, None, native).reconcile_context(self.actor)
        self.assertEqual([item["token"] for item in pending], [token_a])
        self.assertEqual(self.ledger.attempt(token_b)["status"], "admitted")
        self.assertIsNone(self.receipt("A")["announced_at"])
        self.assertIsNotNone(self.receipt("B")["announced_at"])
        self.ledger.mark_read(self.actor, ["A"])
        self.assertTrue(self.ledger.complete(self.actor, TURN, self.epoch))
        finished_b, receipt_b = deepcopy(self.ledger.attempt(token_b)), self.receipt("B")

        # Reopening the coordinator ledger retains reservations and receipts.
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.attempt(token_a)["agent_seen_at"], seen_a["agent_seen_at"])
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])
        with patch("murmur.native.verify_context_receipt", return_value={
                "status": "accepted", "evidence": {"native_record": token_a}}):
            self.assertEqual(Coordinator(self.ledger, None, native).reconcile_context(self.actor), [])
        late_a = self.ledger.attempt(token_a)
        self.assertEqual(late_a["status"], "completed")
        self.assertEqual(late_a["agent_seen_at"], seen_a["agent_seen_at"])
        self.assertEqual(self.ledger.attempt(token_b), finished_b)
        self.assertEqual(self.receipt("B"), receipt_b)
        self.assertIsNone(receipt_b["read_at"])
        self.assertIsNotNone(self.receipt("A")["read_at"])
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_failed_old_idle_recovery_does_not_block_fresh_delivery_or_replay_a_cohort(self):
        self.backend.add("A", when=101)
        handed_off = []

        def send(identity, text):
            attempt = self.ledger.attempt(parse_wake(text))
            handed_off.append((attempt["token"], attempt["post_ids"]))
            return DeliveryResult("uncertain", "Response lost after possible enqueue")

        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity), \
                patch("murmur.native.prepare_idle_handoff", return_value=None), \
                patch("murmur.native.deliver", side_effect=send), \
                patch("murmur.native.owner_process_state", return_value={"status": "unknown"}):
            network_tick(self.root, self.actor, threading.Event())
        original = self.ledger.attempt(handed_off[0][0])
        self.backend.add("B", when=102)

        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity), \
                patch("murmur.native.prepare_idle_handoff", return_value=None), \
                patch("murmur.native.deliver", side_effect=send), \
                patch("murmur.native.owner_process_state", side_effect=OSError("Old owner inspection unavailable")):
            network_tick(self.root, self.actor, threading.Event())
            self.ledger.close()
            self.ledger = Ledger(self.path)
            for _ in range(3):
                network_tick(self.root, self.actor, threading.Event())

        self.assertEqual([posts for _, posts in handed_off], [["A"], ["B"]])
        self.assertEqual(self.ledger.attempt(original["token"]), original)
        self.assertEqual(len(self.ledger.attempts(self.actor)), 2)
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])
        token_b = handed_off[1][0]
        self.ledger.acknowledge_notices(self.actor, [original["token"]])
        self.assertTrue(self.ledger.admit(token_b, self.actor, self.epoch, NEXT_TURN,
                                          native_identity=asdict(self.identity)))
        self.assertTrue(self.ledger.complete(self.actor, NEXT_TURN, self.epoch))
        self.assertEqual(self.ledger.attempt(token_b)["status"], "completed")
        self.assertTrue(all(self.receipt(post)["read_at"] is None for post in ("A", "B")))
        self.assertIsNone(self.receipt("A")["announced_at"])
        self.assertIsNotNone(self.receipt("B")["announced_at"])

    def test_two_connections_reserve_a_later_cohort_once_despite_an_older_uncertainty(self):
        self.add("A", when=101)
        first = self.ledger.create_attempt(self.actor, self.epoch)
        self.ledger.set_transport(first["token"], "uncertain", {"possible_enqueue": True})
        self.add("B", when=102)
        barrier = threading.Barrier(2)

        def claim():
            ledger = Ledger(self.path)
            try:
                barrier.wait(2)
                return ledger.create_attempt(self.actor, self.epoch)
            finally:
                ledger.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim(), range(2)))
        winners = [attempt for attempt in claims if attempt is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["post_ids"], ["B"])
        self.assertIsNone(self.ledger.create_attempt(self.actor, self.epoch))
        self.assertEqual(sorted(attempt["post_ids"] for attempt in self.ledger.attempts(self.actor)),
                         [["A"], ["B"]])
        self.assertTrue(all(self.receipt(post)["announced_at"] is None for post in ("A", "B")))

    def test_agent_receipt_between_wake_claim_and_native_wake_cancels_that_wake(self):
        self.add("A", when=101)
        transport = RestartTransport(self.ledger)
        coordinator = Coordinator(self.ledger, self.backend, transport)
        coordinator.deliver(self.identity)
        attempt, = self.ledger.attempts(self.actor)
        transport.before_final_probe = lambda: self.ledger.acknowledge_notices(self.actor, [attempt["token"]])
        result = coordinator.recover_idle(attempt, self.ledger.actor(self.actor)["identity"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(transport.wakes, [])
        self.assertEqual(len(transport.calls), 1)
        after = self.ledger.attempt(attempt["token"])
        self.assertEqual(after["status"], "pending")
        self.assertIsNotNone(after["agent_seen_at"])
        self.assertIsNone(after["admitted_at"])
        self.assertIsNone(after["verification"])
        self.assertIsNone(self.receipt("A")["read_at"])
        self.assertIsNone(self.receipt("A")["announced_at"])


if __name__ == "__main__":
    unittest.main()
