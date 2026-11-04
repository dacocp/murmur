"""Idle target recovery is an atomic claim, never a retry of held native work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import config, diagnostics, hooks
from murmur.control import network_tick
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import DeliveryResult, NativeIdentity
from test_coordinator import FakeBackend, FakeTransport


HOST = "local_11111111-1111-4111-8111-111111111111"
ENGINE = "22222222-2222-4222-8222-222222222222"


class IdleRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="murmur-idle-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = config.ledger_path(self.root)
        self.now = 100
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.addCleanup(self.ledger.close)
        hooks.initialize(self.ledger.db)
        self.old = NativeIdentity("claude", HOST, parent_known=True,
            target={"runtime": "desktop", "pid": 41, "engine_id": ENGINE,
                    "proc_start": "old birth", "native_epoch": "old epoch",
                    "config_dir": "/fixture/claude", "socket_path": "/fixture/old.sock"},
            evidence={"parent_proof": {"kind": "fixture verified root"},
                      "lineage_complete": True, "lineage_edges": [{"session_id": HOST, "parent_session_id": None}]})
        self.fresh = replace(self.old, target={**self.old.target, "pid": 42,
            "proc_start": "new birth", "native_epoch": "new epoch", "socket_path": "/fixture/new.sock"})
        self.actor = self.old.actor
        self.ledger.enroll(self.actor, asdict(self.old), "Exact native title", "self")
        self.backend = FakeBackend()
        self.backend.add("request", causal={"chain_ids": ["original request"]})
        self.transport = FakeTransport()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.coordinator.sync(self.actor)
        config.save({"url": "http://fixture.invalid", "token": "fixture", "delivery_enabled": True}, self.root)
        self.now = 200
        http = patch("urllib.request.urlopen", side_effect=AssertionError("Idle fixtures must not contact a server"))
        http.start()
        self.addCleanup(http.stop)

    def state(self, ledger=None):
        """Compare protected operational state; diagnostic audit rows may grow."""
        db = (ledger or self.ledger).db
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("actors", "conversations", "posts", "receipts", "attempts",
                              "native_turns", "native_turn_records", "outbound", "onboarding")}

    def claim(self, fresh=None, *, token=None, expected=None):
        fresh = fresh or self.fresh
        return self.ledger.create_attempt(self.actor, routing_binding(fresh), token,
            expected_identity=asdict(self.old) if expected is None else expected,
            refreshed_identity=asdict(fresh))

    def tick(self, prepare, *, owner=None, stopped=None):
        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.prepare_idle_target", side_effect=prepare) as preparation, \
                patch("murmur.native.verify_target", side_effect=AssertionError("Idle scheduling must use its preparation API")), \
                patch("murmur.native.owner_process_state", return_value=owner or {"status": "unknown"}) as old_owner, \
                patch("murmur.native.deliver", return_value=DeliveryResult("accepted", "fixture socket accepted")) as delivery:
            result = network_tick(self.root, self.actor, threading.Event() if stopped is None else stopped)
        return result, preparation, delivery, old_owner

    def test_socket_only_refresh_claims_and_sends_once_with_unchanged_epoch(self):
        fresh = replace(self.old, target={**self.old.target, "socket_path": "/fixture/rebound.sock"})
        self.assertEqual(routing_binding(self.old), routing_binding(fresh))
        before = self.ledger.actor(self.actor)
        result = self.coordinator.deliver(fresh, expected_identity=asdict(self.old))
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "accepted")
        self.assertEqual([call[0] for call in self.transport.calls], [fresh])
        after = self.ledger.actor(self.actor)
        self.assertEqual(after["identity"], asdict(fresh))
        for field in ("actor", "name", "mode", "user_id", "enrolled_at", "enrollment_id", "enrollment_seq", "causal"):
            self.assertEqual(after[field], before[field])
        attempts = self.ledger.attempts(self.actor)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["native_identity"], asdict(fresh))
        self.assertEqual(attempts[0]["epoch"], routing_binding(fresh))
        self.assertEqual(attempts[0]["post_ids"], ["request"])
        self.assertEqual(attempts[0]["causal"], {"chain_ids": ["original request"]})
        self.assertEqual(attempts[0]["transport"], "accepted")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_scheduler_uses_preverification_snapshot_for_a_replacement_idle_owner(self):
        def prepare(recorded):
            self.assertEqual(recorded, self.old)
            return self.fresh  # Native owner-death/registry proof is mocked here.

        _, preparation, delivery, _ = self.tick(prepare)
        preparation.assert_called_once_with(self.old)
        self.assertEqual(delivery.call_count, 1)
        self.assertEqual(delivery.call_args.args[0], self.fresh)
        attempt = self.ledger.attempts(self.actor)[0]
        self.assertEqual(attempt["native_identity"], asdict(self.fresh))
        self.assertEqual(self.ledger.actor(self.actor)["identity"], asdict(self.fresh))

    def test_completed_newer_hook_wins_the_verification_race_and_next_tick_can_deliver(self):
        newest = replace(self.fresh, target={**self.fresh.target, "pid": 43,
                         "native_epoch": "newest epoch", "socket_path": "/fixture/newest.sock"})
        expected = []

        def prepare(recorded):
            self.assertEqual(recorded, self.old)
            with closing(Ledger(self.path)) as concurrent:
                concurrent.start_turn(self.actor, routing_binding(newest), "newer hook turn", native_identity=asdict(newest))
                concurrent.complete(self.actor, "newer hook turn", routing_binding(newest))
                expected.append(self.state(concurrent))
            return self.fresh

        _, _, delivery, _ = self.tick(prepare)
        delivery.assert_not_called()
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertEqual(self.state(), expected[0])
        _, preparation, delivery, _ = self.tick(lambda recorded: recorded)
        preparation.assert_called_once_with(newest)
        self.assertEqual(delivery.call_count, 1)
        self.assertEqual(delivery.call_args.args[0], newest)

    def test_disabling_delivery_during_preparation_prevents_claim_refresh_and_send(self):
        before = self.state()

        def prepare(recorded):
            settings = config.load(self.root)
            settings["delivery_enabled"] = False
            config.save(settings, self.root)
            return self.fresh

        _, preparation, delivery, _ = self.tick(prepare)
        preparation.assert_called_once_with(self.old)
        delivery.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_shutdown_during_preparation_prevents_claim_refresh_and_send(self):
        before = self.state()
        stopped = threading.Event()

        def prepare(recorded):
            stopped.set()
            return self.fresh

        _, preparation, delivery, _ = self.tick(prepare, stopped=stopped)
        preparation.assert_called_once_with(self.old)
        delivery.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_active_turn_prevents_preparation_and_identity_refresh(self):
        self.ledger.start_turn(self.actor, routing_binding(self.old), "current turn", native_identity=asdict(self.old))
        before = self.state()
        _, preparation, delivery, _ = self.tick(lambda _: self.fresh)
        preparation.assert_not_called()
        delivery.assert_not_called()
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(), before)

    def test_pending_accepted_and_uncertain_idle_work_cannot_be_rebound(self):
        attempt = self.ledger.create_attempt(self.actor, routing_binding(self.old), "held idle")
        for status in (None, "accepted", "uncertain"):
            if status:
                self.ledger.set_transport(attempt["token"], status, {"target": self.old.target, "detail": "old native result"})
            before = self.state()
            with self.subTest(transport=status):
                _, preparation, delivery, _ = self.tick(lambda _: self.fresh)
                preparation.assert_not_called()
                delivery.assert_not_called()
                self.assertIsNone(self.claim())
                self.assertEqual(self.state(), before)

    def test_admitted_old_work_and_emitted_unconfirmed_context_keep_their_owner(self):
        attempt = self.ledger.create_attempt(self.actor, routing_binding(self.old), "admitted idle")
        self.ledger.admit(attempt["token"], self.actor, routing_binding(self.old), "admitted turn", native_identity=asdict(self.old))
        before = self.state()
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(), before)
        self.ledger.complete(self.actor, "admitted turn", routing_binding(self.old))
        self.backend.add("later request", when=102)
        self.coordinator.sync(self.actor)
        self.ledger.start_turn(self.actor, routing_binding(self.old), "busy turn", native_identity=asdict(self.old))
        context = self.ledger.claim_context(self.actor, routing_binding(self.old), "busy turn", "tool",
                                           native_identity=asdict(self.old), token="emitted context")
        self.ledger.mark_context_emitted(context["token"], self.actor, routing_binding(self.old), "busy turn", "tool")
        self.ledger.complete(self.actor, "busy turn", routing_binding(self.old))
        self.assertIsNone(self.ledger.active_turn(self.actor))
        before = self.state()
        _, preparation, delivery, _ = self.tick(lambda _: self.fresh)
        preparation.assert_not_called()
        delivery.assert_not_called()
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ledger.attempt(context["token"])["native_identity"], asdict(self.old))

    def test_positive_old_owner_exit_does_not_retire_or_resend_held_input(self):
        self.ledger.create_attempt(self.actor, routing_binding(self.old), "possibly queued")
        self.ledger.set_transport("possibly queued", "uncertain", {"target": self.old.target})
        before = self.state()
        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.owner_process_state", return_value={"status": "ended"}), \
                patch("murmur.native.prepare_idle_target") as prepare, patch("murmur.native.deliver") as deliver:
            network_tick(self.root, self.actor, threading.Event())
        prepare.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_epoch_mismatch_preserves_old_input_without_rebinding_or_resending_it(self):
        self.ledger.create_attempt(self.actor, routing_binding(self.old), "old epoch token")
        self.ledger.update_identity(self.actor, asdict(self.fresh))
        before = self.state()
        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.owner_process_state", return_value={"status": "unknown"}), \
                patch("murmur.native.prepare_idle_target") as prepare, patch("murmur.native.deliver") as deliver:
            network_tick(self.root, self.actor, threading.Event())
        prepare.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_late_read_or_mute_prevents_both_refresh_and_claim(self):
        for action in ("read", "mute"):
            with self.subTest(action=action):
                if action == "mute":
                    self.ledger.db.execute("UPDATE receipts SET read_at=NULL WHERE actor=?", (self.actor,))
                expected = []

                def prepare(recorded):
                    with closing(Ledger(self.path)) as concurrent:
                        if action == "read":
                            concurrent.mark_read(self.actor, ["request"])
                        else:
                            concurrent.set_mute(self.actor, "build", True)
                        expected.append(self.state(concurrent))
                    return self.fresh

                _, preparation, delivery, _ = self.tick(prepare)
                preparation.assert_called_once_with(self.old)
                delivery.assert_not_called()
                self.assertEqual(self.state(), expected[0])

    def test_manual_pending_onboarding_and_empty_inbox_do_not_refresh(self):
        manual = NativeIdentity("codex", "33333333-3333-4333-8333-333333333333", target={"cwd": "/fixture/manual"})
        pending = replace(self.old, session_id="local_44444444-4444-4444-8444-444444444444", parent_session_id=HOST)
        for identity, mode, onboarding in ((manual, "manual", None),
                                           (pending, "native", ("pending", self.actor))):
            with self.subTest(mode=mode):
                self.ledger.enroll(identity.actor, asdict(identity), "Own enrollment", "self", mode=mode, onboarding=onboarding)
                self.ledger.sync_conversations(identity.actor, self.backend.conversations(identity.actor))
                self.ledger.ingest(identity.actor, [self.backend.canonical["request"]], 1, 1)
                candidate = replace(identity, target={**identity.target, "socket_path": "/fixture/replacement.sock"})
                before = self.state()
                result = self.ledger.create_attempt(identity.actor, routing_binding(candidate),
                    expected_identity=asdict(identity), refreshed_identity=asdict(candidate))
                self.assertIsNone(result)
                self.assertEqual(self.state(), before)
        self.ledger.mark_read(self.actor, ["request"])
        before = self.state()
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(), before)

    def test_refresh_requires_old_cas_and_cannot_change_actor_parent_or_proof(self):
        before = self.state()
        with self.assertRaises(LedgerError):
            self.ledger.create_attempt(self.actor, routing_binding(self.fresh), refreshed_identity=asdict(self.fresh))
        for candidate in (replace(self.fresh, session_id="local_55555555-5555-4555-8555-555555555555"),
                          replace(self.fresh, parent_session_id="local_66666666-6666-4666-8666-666666666666"),
                          replace(self.fresh, parent_known=False),
                          replace(self.fresh, evidence={"parent_proof": "changed"})):
            with self.subTest(candidate=candidate), self.assertRaises(LedgerError):
                self.claim(candidate)
            self.assertEqual(self.state(), before)

    def test_competing_connections_claim_and_send_only_once(self):
        barrier = threading.Barrier(2, timeout=5)

        def contender():
            with closing(Ledger(self.path, clock=lambda: 200)) as ledger:
                coordinator = Coordinator(ledger, self.backend, self.transport)
                barrier.wait()
                return coordinator.deliver(self.fresh, expected_identity=asdict(self.old))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][0], self.fresh)
        self.assertEqual(len(self.ledger.attempts(self.actor)), 1)
        self.assertEqual(self.ledger.actor(self.actor)["identity"], asdict(self.fresh))

    def test_failed_attempt_insert_rolls_back_the_target_refresh(self):
        self.ledger.db.executescript("""CREATE TRIGGER reject_idle_claim AFTER INSERT ON attempts
            WHEN NEW.mode='idle' BEGIN SELECT RAISE(ABORT,'fixture claim insert failed'); END;""")
        before = self.state()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture claim insert failed"):
            self.claim(token="rollback")
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ledger.db.in_transaction)

    def test_doctor_distinguishes_verified_candidate_from_recorded_target_without_refreshing(self):
        self.backend.request = lambda *_args: {"version": "fixture"}
        before = self.state()
        with patch("murmur.native.verify_target", return_value=self.fresh), \
                patch.object(diagnostics, "route_capabilities", return_value={}):
            result = diagnostics.report(self.coordinator, self.old, {"delivery_enabled": True})
        self.assertTrue(result["native"]["ok"])
        self.assertEqual(result["native"]["recorded_target"], self.old.target)
        self.assertEqual(result["native"]["target"], self.fresh.target)
        self.assertFalse(result["native"]["target_matches_recorded"])
        self.assertIsNone(result["last_admission"])
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
