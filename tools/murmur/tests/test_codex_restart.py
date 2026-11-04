"""Persisted recovery intent cannot become a second native enqueue or admission."""

from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from murmur import diagnostics, hooks
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError
from murmur.native import DeliveryResult, NativeIdentity
from test_coordinator import FakeBackend, FakeTransport


SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "codex:22222222-2222-4222-8222-222222222222"
PAYLOAD_SHA = "a" * 64


class RestartTransport(FakeTransport):
    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.wakes = []
        self.probes = []
        self.before_probe = self.before_wake = None
        self.before_final_probe = self.before_prepare = None
        self.probe_result = {"status": "ready", "queue_id": "retained-native-id",
                             "queue_payload_sha256": PAYLOAD_SHA,
                             "successor": {"app_pid": 20, "backend_pid": 21}}

    def prepare_idle_handoff(self, identity, message, token, *, state_root):
        if self.before_prepare:
            self.before_prepare()
        account = self.ledger.actor(identity.key)
        return {"schema": 1, "kind": "codex-idle-handoff-v1", "actor": identity.key,
                "session_id": identity.session_id, "token": token, "text": message,
                "gate": {"fixture": "trusted owned Submit; not a native fail-closed guarantee",
                         "ledger": {key: account[key] for key in ("enrollment_id", "enrollment_seq", "user_id")}}}

    def deliver(self, identity, message):
        attempt, = self.ledger.pending_attempts(identity.key)
        assert attempt["emission"]["text"] == message  # Must precede any enqueue effect.
        self.calls.append((identity, message))
        return DeliveryResult("uncertain", "Original enqueue response lost")

    def owner_process_state(self, identity, *, timeout):
        return {"status": "ended"}

    def probe_idle_recovery(self, identity, attempt, *, state_root):
        self.probes.append((identity, attempt["token"]))
        if self.before_probe:
            self.before_probe()
        return dict(self.probe_result)

    def wake_idle_recovery(self, session_id, probe, *, before_wake=None):
        if self.before_final_probe:
            self.before_final_probe()
        if before_wake is not None and not before_wake():
            return DeliveryResult("rejected", "Current local claim no longer permits wake")
        self.wakes.append((session_id, probe["queue_id"]))
        if self.before_wake:
            self.before_wake()
        return DeliveryResult("accepted", "Wake URL scheduled; no input appended")


class CodexRestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.now = 100
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.ledger.close())
        self.backend = FakeBackend()
        self.native = RestartTransport(self.ledger)
        self.coordinator = Coordinator(self.ledger, self.backend, self.native)
        self.identity = NativeIdentity("codex", SESSION, parent_known=True,
                                       evidence={"desktop_wake": {"fixture": "old owner"}})
        self.actor = self.identity.key
        self.coordinator.register(self.identity, "Restart fixture")
        self.backend.add("original")
        self.coordinator.sync(self.actor)
        self.coordinator.deliver(self.identity)
        self.original, = self.ledger.attempts(self.actor)

    def recover(self, *, enabled=None):
        return self.coordinator.recover_idle(self.ledger.attempt(self.original["token"]),
                                             self.ledger.actor(self.actor)["identity"], enabled=enabled)

    def test_same_queue_entry_is_woken_without_reenqueue_and_later_arrival_is_independent(self):
        def admit_before_wake_response():
            fresh = replace(self.identity, evidence={"desktop_wake": {"fixture": "successor"}})
            self.ledger.admit(self.original["token"], self.actor, self.original["epoch"], TURN,
                              native_identity=asdict(fresh))
            self.ledger.complete(self.actor, TURN, self.original["epoch"])
        self.native.before_wake = admit_before_wake_response
        self.recover()
        after = self.ledger.attempt(self.original["token"])
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["turn_key"], TURN)
        for field in ("native_identity", "emission", "post_ids", "token", "transport"):
            self.assertEqual(after[field], self.original[field])
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self.native.wakes, [(SESSION, "retained-native-id")])
        self.assertIsNone(self.recover())
        self.backend.add("later", when=102)
        self.coordinator.sync(self.actor)
        fresh = NativeIdentity(**self.ledger.actor(self.actor)["identity"])
        self.coordinator.deliver(fresh)
        later, = [attempt for attempt in self.ledger.attempts(self.actor)
                  if attempt["token"] != self.original["token"]]
        self.assertEqual(later["post_ids"], ["later"])
        self.assertNotEqual(later["token"], self.original["token"])
        self.assertEqual(later["native_identity"], asdict(fresh))
        self.assertEqual(len(self.native.calls), 2)

    def test_coordinator_restart_retries_only_wake_after_lost_response(self):
        def lose_response():
            raise OSError("IPC response lost after URL scheduling")
        self.native.before_wake = lose_response
        with self.assertRaises(OSError):
            self.recover()
        self.assertEqual(self.ledger.attempt(self.original["token"])["evidence"]["recovery"]["status"], "uncertain")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.native.ledger = self.ledger
        self.coordinator = Coordinator(self.ledger, self.backend, self.native)
        self.native.before_wake = None
        self.assertIsNone(self.recover())  # Same durable lease survives this restart.
        self.now += 10001
        self.recover()
        self.assertEqual(self.native.wakes, [(SESSION, "retained-native-id")] * 2)
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self.ledger.attempt(self.original["token"])["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_absence_duplicate_and_inaccessible_queue_are_holds_without_effects(self):
        for reason in ("queue absent", "two matching entries", "queue inaccessible", "history contradiction"):
            with self.subTest(reason=reason):
                self.native.probe_result = {"status": "held", "reason": reason}
                self.assertEqual(self.recover(), {"status": "held", "reason": reason})
                self.assertEqual(self.ledger.attempt(self.original["token"]), self.original)
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self.native.wakes, [])

    def test_muting_prevents_wake(self):
        self.native.before_probe = lambda: self.ledger.set_mute(self.actor, "build", True)
        self.assertIsNone(self.recover())
        self.assertEqual(self.native.wakes, [])

    def test_identity_change_while_probing_prevents_wake(self):
        self.assertEqual([post["id"] for post in self.ledger._unread(self.actor, eligible=True)], ["original"])
        fresh = asdict(replace(self.identity, evidence={"fixture": "genuine capture raced"}))
        self.native.before_probe = lambda: self.ledger.refresh_native_evidence(
            self.actor, asdict(self.identity), fresh)
        self.assertIsNone(self.recover())
        self.assertEqual(self.native.wakes, [])
        self.assertEqual(self.ledger.attempt(self.original["token"]), self.original)

    def test_disable_race_and_retirement_cannot_revive_a_token(self):
        answers = iter((True, False))
        self.assertIsNone(self.recover(enabled=lambda: next(answers)))
        self.assertEqual(self.native.wakes, [])
        self.ledger.retire_attempt(self.original["token"], "Explicit operator decision")
        before = self.ledger.attempt(self.original["token"])
        self.assertIsNone(self.recover())
        self.assertEqual(self.ledger.attempt(self.original["token"]), before)

    def test_changes_during_final_native_reprobe_prevent_url_dispatch(self):
        for change in ("mute", "retire", "identity", "enrollment", "admit"):
            with self.subTest(change=change):
                # Independent actor/ledger per interleaving, not a revived token.
                case = CodexRestartTests()
                case.setUp()
                try:
                    def mutate():
                        if change == "mute":
                            case.ledger.set_mute(case.actor, "build", True)
                        elif change == "retire":
                            case.ledger.retire_attempt(case.original["token"], "Operator decision")
                        elif change == "identity":
                            case.ledger.refresh_native_evidence(case.actor, asdict(case.identity),
                                asdict(replace(case.identity, evidence={"fixture": "fresh capture"})))
                        elif change == "enrollment":
                            case.ledger.db.execute("UPDATE actors SET enrollment_id='replacement' WHERE actor=?", (case.actor,))
                        else:
                            case.ledger.admit(case.original["token"], case.actor, case.original["epoch"], TURN)
                    case.native.before_final_probe = mutate
                    case.recover()
                    self.assertEqual(case.native.wakes, [])
                    self.assertEqual(len(case.native.calls), 1)
                    if change == "admit":
                        self.assertEqual(case.ledger.attempt(case.original["token"])["status"], "admitted")
                finally:
                    case.doCleanups()

    def test_identity_change_between_claim_and_seal_prevents_initial_enqueue(self):
        self.backend.add("later", when=102)
        self.coordinator.sync(self.actor)
        self.ledger.retire_attempt(self.original["token"], "Separate completed test")
        fresh = asdict(replace(self.identity, evidence={"fixture": "new capture"}))
        self.native.before_prepare = lambda: self.ledger.refresh_native_evidence(
            self.actor, asdict(self.identity), fresh)
        self.assertIsNone(self.coordinator.deliver(self.identity))
        self.assertEqual(len(self.native.calls), 1)
        self.assertFalse(any(attempt["status"] == "pending" for attempt in self.ledger.attempts(self.actor)))

    def test_lease_cas_and_retained_queue_id_survive_retries(self):
        identity = self.ledger.actor(self.actor)["identity"]
        first = self.ledger.claim_idle_wake(self.original["token"], self.original, identity,
                                             {"queue_id": "retained", "generation": "new", "queue_payload_sha256": PAYLOAD_SHA})
        self.assertIsNotNone(first)
        self.assertIsNone(self.ledger.claim_idle_wake(self.original["token"], self.original, identity,
                                                      {"queue_id": "retained", "generation": "new", "queue_payload_sha256": PAYLOAD_SHA}))
        current = self.ledger.attempt(self.original["token"])
        self.assertIsNone(self.ledger.claim_idle_wake(self.original["token"], current, identity,
                                                      {"queue_id": "retained", "generation": "new", "queue_payload_sha256": PAYLOAD_SHA}))
        self.now += 10001
        with self.assertRaises(LedgerError):
            self.ledger.claim_idle_wake(self.original["token"], current, identity,
                                        {"queue_id": "different", "generation": "new", "queue_payload_sha256": PAYLOAD_SHA})
        with self.assertRaisesRegex(LedgerError, "payload"):
            self.ledger.claim_idle_wake(self.original["token"], current, identity,
                {"queue_id": "retained", "generation": "new", "queue_payload_sha256": "b" * 64})
        second = self.ledger.claim_idle_wake(self.original["token"], current, identity,
                                             {"queue_id": "retained", "generation": "new", "queue_payload_sha256": PAYLOAD_SHA})
        self.assertNotEqual(first["action_id"], second["action_id"])
        self.assertFalse(self.ledger.finish_idle_wake(self.original["token"], first["action_id"],
                                                     {"status": "accepted"}))

    def test_stale_source_diagnostic_uses_verified_enrollment_not_light_capture(self):
        captured = replace(self.identity, evidence={
            "codex_thread_source": {"subAgent": {"thread_spawn": {
                "parent_thread_id": "33333333-3333-4333-8333-333333333333", "depth": 1}}},
            "codex_thread_source_session_id": SESSION,
            "codex_thread_source_origin": "app-server:thread/read"})
        self.ledger.update_identity(self.actor, asdict(captured))
        hooks.initialize(self.ledger.db)
        backend = Mock()
        backend.request.return_value = {"version": "fixture"}
        before = tuple(self.ledger.db.iterdump())
        with patch.object(diagnostics.native, "verify_target", side_effect=ValueError("Owner ended")):
            result = diagnostics.report(Coordinator(self.ledger, backend), self.identity,
                                        {"delivery_enabled": True, "busy_context_enabled": True})
        self.assertFalse(result["native"]["ok"])
        self.assertEqual(result["routes"]["idle_queue"]["status"], "unsupported")
        self.assertIn("Spawned", result["routes"]["idle_queue"]["reason"])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_diagnostic_exposes_wake_progress_without_consuming_or_announcing(self):
        self.recover()
        hooks.initialize(self.ledger.db)
        backend = Mock()
        backend.request.return_value = {"version": "fixture"}
        before = tuple(self.ledger.db.iterdump())
        with patch.object(diagnostics.native, "verify_target", return_value=self.identity), \
                patch.object(diagnostics, "route_capabilities", return_value={
                    "idle_queue": {"status": "supported"}, "busy_context": {"status": "unverified"}}):
            report = diagnostics.report(Coordinator(self.ledger, backend), self.identity,
                                        {"delivery_enabled": True})
        attempt, = report["attempts"]
        self.assertTrue(attempt["restart_qualified"])
        self.assertEqual(attempt["idle_recovery"]["queue_id"], "retained-native-id")
        self.assertEqual(attempt["idle_recovery"]["status"], "accepted")
        self.assertEqual(attempt["status"], "pending")
        self.assertIsNone(attempt["admitted_at"])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
