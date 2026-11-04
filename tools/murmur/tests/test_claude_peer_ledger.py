"""Corroborated Claude peer admission against a real, isolated SQLite ledger."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur.coordinator import routing_binding
from murmur.ledger import Ledger, LedgerError


class ClaudePeerLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.now = 1000
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.addCleanup(self.ledger.close)
        self.identity = self.native_identity(1)
        self.actor = self.actor_key(self.identity)
        self.binding = routing_binding(self.identity)
        self.expected = self.turn_key(self.identity, 1)
        self.sequence = 0
        self.enroll(self.identity)

    @staticmethod
    def native_identity(number, *, epoch=None):
        engine = f"{number:08d}-1111-4111-8111-111111111111"
        host = f"local_{number:08d}-2222-4222-8222-222222222222"
        return {"provider": "claude", "session_id": host,
                "parent_known": True, "parent_session_id": None,
                "target": {"runtime": "desktop", "entrypoint": "claude-vscode", "engine_id": engine,
                           "pid": 123, "proc_start": "native process birth", "config_dir": "/native/.claude",
                           "native_epoch": epoch or "raw-native-owner-epoch:" + engine},
                "evidence": {"source": "synthetic native identity"}}

    @staticmethod
    def actor_key(identity):
        return identity["provider"] + ":" + identity["session_id"]

    @staticmethod
    def turn_key(identity, number):
        prompt = f"{number:08d}-3333-4333-8333-333333333333"
        return "claude:" + identity["target"]["engine_id"] + ":" + prompt

    def enroll(self, identity, *, mode="native", user_id="registered-user", onboarding=None):
        actor = self.actor_key(identity)
        self.ledger.enroll(actor, identity, "Claude receiver", user_id, mode=mode, onboarding=onboarding)
        self.ledger.sync_conversations(actor, [
            {"id": "build", "name": "Build", "kind": "channel", "role": "member"}])
        return actor

    def post(self, post_id="initial", *, actor=None):
        actor = actor or self.actor
        self.sequence += 1
        self.ledger.ingest(actor, [{"id": post_id, "channel_id": "build", "user_id": "peer-user",
                                   "author": "Colleague", "root_id": "", "create_at": 1001,
                                   "causal": {"chain_ids": ["origin:" + post_id]}}], self.sequence, 1)

    def request(self, *, identity=None, expected=None):
        identity = identity or self.identity
        expected = expected or self.turn_key(identity, 1)
        return {"schema": 1, "kind": "claude-peer-v1", "actor": self.actor_key(identity),
                "session_id": identity["session_id"], "engine_id": identity["target"]["engine_id"],
                "epoch": identity["target"]["native_epoch"], "turn_key": expected,
                "prompt_id": expected.rsplit(":", 1)[1],
                "profile": {"runtime": "desktop", "version": "synthetic measured profile", "sha256": "a" * 64}}

    def start(self, *, identity=None, turn=None):
        identity = identity or self.identity
        return self.ledger.start_turn(self.actor_key(identity), routing_binding(identity), turn or self.turn_key(identity, 1),
                                      native_identity=identity)

    def claim(self, *, identity=None, expected=None, request=None, token=None, expected_identity=None):
        identity = identity or self.identity
        return self.ledger.claim_peer(self.actor_key(identity), routing_binding(identity), expected or self.turn_key(identity, 1),
                                      native_identity=identity,
                                      receipt_request=request or self.request(identity=identity, expected=expected),
                                      expected_identity=expected_identity or identity, token=token)

    def emission(self, attempt):
        encoded = json.dumps(attempt["receipt_request"], sort_keys=True, separators=(",", ":")).encode()
        return {"schema": 1, "kind": "claude-peer-v1", "actor": attempt["actor"],
                "request_sha256": hashlib.sha256(encoded).hexdigest(),
                "message_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "murmur:" + attempt["token"])),
                "text": "You have new messages. [wake:" + attempt["token"] + "]",
                "from": "uds:/safe/reply.sock", "producer_pid": 123, "frame_sha256": "b" * 64}

    def seal(self, attempt, *, emission=None):
        return self.ledger.mark_peer_emitted(attempt["token"], attempt["actor"], attempt["epoch"],
                                             attempt["expected_turn_key"], emission or self.emission(attempt))

    def evidence(self, attempt, *, actual=None, delivery="queued_command"):
        actual = actual or attempt["expected_turn_key"]
        return {"source": "claude-peer-v1", "actor": attempt["actor"], "token": attempt["token"],
                "epoch": attempt["receipt_request"]["epoch"],
                "expected_turn_key": attempt["expected_turn_key"], "actual_turn_key": actual,
                "turn_key": actual, "message_id": self.emission(attempt)["message_id"],
                "receipt_uuid": str(uuid.uuid4()), "byte_start": 0,
                "profile": attempt["receipt_request"]["profile"], "delivery": delivery}

    def confirm(self, attempt, *, evidence=None):
        return self.ledger.confirm_peer(attempt["token"], attempt["actor"], attempt["epoch"],
                                        attempt["expected_turn_key"], verification=evidence or self.evidence(attempt))

    def submit(self, attempt, actual, *, identity=None):
        return self.ledger.record_peer_submit(attempt["token"], attempt["actor"], attempt["epoch"], actual,
                                              native_identity=identity or self.identity)

    def receipt(self, post_id="initial", *, actor=None):
        return dict(self.ledger.db.execute("SELECT * FROM receipts WHERE actor=? AND post_id=?",
                                          (actor or self.actor, post_id)).fetchone())

    def pending(self):
        self.post()
        self.start()
        return self.claim()

    def test_same_prompt_receipt_admits_once_and_preserves_original_turn(self):
        attempt = self.pending()
        original = self.ledger.active_turn(self.actor)
        self.assertEqual((attempt["mode"], attempt["tool_id"], attempt["status"]), ("context", None, "pending"))
        self.assertNotEqual(attempt["epoch"], attempt["receipt_request"]["epoch"])
        self.assertTrue(self.seal(attempt))
        self.assertFalse(self.seal(attempt))
        self.ledger.set_peer_transport(attempt["token"], "accepted", {"native_status": "delivered"})
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertIsNone(self.receipt()["announced_at"])
        evidence = self.evidence(attempt)
        self.assertTrue(self.confirm(attempt, evidence=evidence))
        self.assertFalse(self.confirm(attempt, evidence=evidence))
        admitted = self.ledger.attempt(attempt["token"])
        self.assertEqual((admitted["status"], admitted["turn_key"]), ("admitted", self.expected))
        self.assertEqual(self.ledger.active_turn(self.actor), original)
        self.assertIsNotNone(self.receipt()["announced_at"])
        self.assertTrue(self.ledger.complete(self.actor, self.expected, self.binding))
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")
        self.assertFalse(self.confirm(attempt, evidence=evidence))
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_claim_requires_enrollment_automatic_mode_account_and_finished_onboarding(self):
        missing = self.native_identity(9)
        with self.assertRaises(LedgerError):
            self.claim(identity=missing)
        options = ({"mode": "manual"}, {"user_id": None}, {"onboarding": ("unread", self.actor)})
        for number, policy in enumerate(options, 2):
            identity = self.native_identity(number)
            actor = self.enroll(identity, **policy)
            self.post("guard-" + str(number), actor=actor)
            self.start(identity=identity)
            with self.subTest(policy=policy):
                self.assertIsNone(self.claim(identity=identity))
                self.assertEqual(self.ledger.attempts(actor), [])
                self.assertIsNone(self.receipt("guard-" + str(number), actor=actor)["announced_at"])

    def test_claim_cas_does_not_overwrite_refreshed_identity_or_change_owner(self):
        self.post()
        self.start()
        old = deepcopy(self.identity)
        fresh = {**self.identity, "evidence": {"source": "new native observation"}}
        self.ledger.update_identity(self.actor, fresh)
        self.assertIsNone(self.claim(identity=fresh, expected_identity=old))
        self.assertEqual(self.ledger.actor(self.actor)["identity"], fresh)
        self.assertEqual(self.ledger.attempts(self.actor), [])
        changed = deepcopy(fresh)
        changed["target"]["pid"] += 1
        self.assertIsNone(self.claim(identity=changed, expected_identity=fresh))
        attempt = self.claim(identity=fresh, expected_identity=fresh)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt["native_identity"], fresh)

    def test_claim_needs_exact_active_turn_raw_epoch_request_and_current_owner(self):
        self.post()
        self.assertIsNone(self.claim())
        self.start()
        self.assertIsNone(self.claim(expected=self.turn_key(self.identity, 2)))
        with self.assertRaises(LedgerError):
            self.ledger.claim_peer(self.actor, "wrong-routing-binding", self.expected,
                                   native_identity=self.identity, receipt_request=self.request(),
                                   expected_identity=self.identity)
        for field, value in (("actor", "claude:another"), ("epoch", self.binding),
                             ("engine_id", str(uuid.uuid4())), ("prompt_id", str(uuid.uuid4())),
                             ("kind", "other-context")):
            request = {**self.request(), field: value}
            with self.subTest(field=field), self.assertRaises(LedgerError):
                self.claim(request=request)
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], self.expected)

    def test_confirmation_requires_seal_and_cannot_use_generic_context_admission(self):
        attempt = self.pending()
        with self.assertRaises(LedgerError):
            self.confirm(attempt)
        self.seal(attempt)
        with self.assertRaises(LedgerError):
            self.ledger.confirm_context(attempt["token"], self.actor, self.binding, self.expected, None,
                                        verification=self.evidence(attempt))
        self.assertIsNone(self.receipt()["announced_at"])
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertTrue(self.confirm(attempt))

    def test_emission_seal_rejects_wrong_request_recipient_token_message_or_frame(self):
        attempt = self.pending()
        for field, value in (("request_sha256", "0" * 64), ("actor", "claude:other"),
                             ("text", "You have new messages. [wake:another_token]"),
                             ("message_id", str(uuid.uuid4())), ("frame_sha256", "not-a-sha256")):
            emission = {**self.emission(attempt), field: value}
            with self.subTest(field=field), self.assertRaises(LedgerError):
                self.seal(attempt, emission=emission)
            self.assertIsNone(self.ledger.attempt(attempt["token"])["emitted_at"])
        self.assertTrue(self.seal(attempt))

    def test_wrong_receipt_tuple_never_announces_or_mutates_the_pending_attempt(self):
        attempt = self.pending()
        self.seal(attempt)
        original = self.ledger.attempt(attempt["token"])
        changes = (("source", "codex-canonical-jsonl"), ("actor", "claude:other"),
                   ("token", "other_token"), ("epoch", self.binding),
                   ("expected_turn_key", self.turn_key(self.identity, 2)),
                   ("message_id", str(uuid.uuid4())), ("profile", {"sha256": "different"}),
                   ("receipt_uuid", "not-a-uuid"), ("byte_start", -1), ("delivery", "user_prompt"))
        for field, value in changes:
            with self.subTest(field=field), self.assertRaises(LedgerError):
                self.confirm(attempt, evidence={**self.evidence(attempt), field: value})
            self.assertEqual(self.ledger.attempt(attempt["token"]), original)
            self.assertIsNone(self.receipt()["announced_at"])
        actual = self.turn_key(self.identity, 2)
        with self.assertRaises(LedgerError):
            self.confirm(attempt, evidence=self.evidence(attempt, actual=actual, delivery="peer_prompt"))
        for actor, binding, expected in (("claude:other", self.binding, self.expected),
                                         (self.actor, "wrong-binding", self.expected),
                                         (self.actor, self.binding, actual)):
            with self.subTest(actor=actor, binding=binding, expected=expected), self.assertRaises(LedgerError):
                self.ledger.confirm_peer(attempt["token"], actor, binding, expected,
                                         verification=self.evidence(attempt))

    def test_original_stop_before_peer_submit_does_not_finish_later_admission(self):
        attempt = self.pending()
        self.seal(attempt)
        self.assertTrue(self.ledger.complete(self.actor, self.expected, self.binding))
        actual = self.turn_key(self.identity, 2)
        self.assertTrue(self.submit(attempt, actual))
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertIsNone(self.receipt()["announced_at"])
        self.assertTrue(self.confirm(attempt, evidence=self.evidence(attempt, actual=actual, delivery="peer_prompt")))
        admitted = self.ledger.attempt(attempt["token"])
        self.assertEqual((admitted["turn_key"], admitted["expected_turn_key"], admitted["status"]),
                         (actual, self.expected, "admitted"))
        self.assertFalse(self.ledger.complete(self.actor, self.expected, self.binding))
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], actual)
        self.assertTrue(self.ledger.complete(self.actor, actual, self.binding))
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")

    def test_peer_actual_stop_before_delayed_receipt_never_recreates_a_turn(self):
        attempt = self.pending()
        self.seal(attempt)
        actual = self.turn_key(self.identity, 2)
        self.submit(attempt, actual)
        self.assertFalse(self.ledger.complete(self.actor, self.expected, self.binding))
        self.assertTrue(self.ledger.complete(self.actor, actual, self.binding))
        self.assertIsNone(self.ledger.active_turn(self.actor))
        other = Ledger(self.path, clock=lambda: self.now)
        try:
            self.assertTrue(other.confirm_peer(attempt["token"], self.actor, self.binding, self.expected,
                                               verification=self.evidence(attempt, actual=actual, delivery="peer_prompt")))
            self.assertEqual(other.attempt(attempt["token"])["status"], "completed")
            self.assertIsNone(other.active_turn(self.actor))
        finally:
            other.close()
        self.assertFalse(self.confirm(attempt, evidence=self.evidence(attempt, actual=actual, delivery="peer_prompt")))

    def test_original_stop_before_same_prompt_delayed_receipt_settles_without_recreation(self):
        attempt = self.pending()
        self.seal(attempt)
        self.ledger.complete(self.actor, self.expected, self.binding)
        self.assertTrue(self.confirm(attempt))
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_finished_frozen_owner_receipt_survives_current_actor_refresh_without_resurrection(self):
        attempt = self.pending()
        self.seal(attempt)
        actual = self.turn_key(self.identity, 2)
        self.submit(attempt, actual)
        self.ledger.complete(self.actor, actual, self.binding)
        refreshed = deepcopy(self.identity)
        refreshed["target"].update(native_epoch="new current native owner", pid=456)
        self.ledger.update_identity(self.actor, refreshed)
        self.assertTrue(self.confirm(attempt, evidence=self.evidence(attempt, actual=actual, delivery="peer_prompt")))
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")
        self.assertEqual(self.ledger.actor(self.actor)["identity"], refreshed)
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_actual_turn_record_under_new_owner_cannot_admit_a_frozen_old_owner_packet(self):
        attempt = self.pending()
        self.seal(attempt)
        self.ledger.complete(self.actor, self.expected, self.binding)
        refreshed = deepcopy(self.identity)
        refreshed["target"].update(native_epoch="new current native owner", pid=456)
        actual = self.turn_key(refreshed, 2)
        self.start(identity=refreshed, turn=actual)
        before = self.ledger.active_turn(self.actor)
        with self.assertRaises(LedgerError):
            self.confirm(attempt, evidence=self.evidence(attempt, actual=actual, delivery="peer_prompt"))
        self.assertEqual(self.ledger.active_turn(self.actor), before)
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertIsNone(self.receipt()["announced_at"])

    def test_submit_observation_does_not_admit_and_token_replay_cannot_rebind_actual_turn(self):
        attempt = self.pending()
        self.seal(attempt)
        actual = self.turn_key(self.identity, 2)
        self.submit(attempt, actual)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], actual)
        self.assertIsNone(self.ledger.active_turn(self.actor)["token"])
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertIsNone(self.receipt()["announced_at"])
        self.assertTrue(self.confirm(attempt, evidence=self.evidence(attempt, actual=actual, delivery="peer_prompt")))
        bound = self.ledger.attempt(attempt["token"])
        self.assertFalse(self.submit(attempt, actual))
        with self.assertRaises(LedgerError):
            self.submit(attempt, self.turn_key(self.identity, 3))
        self.assertEqual(self.ledger.attempt(attempt["token"]), bound)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], actual)
        self.ledger.complete(self.actor, actual, self.binding)
        self.assertFalse(self.submit(attempt, actual))
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_native_peer_submit_cannot_change_frozen_owner_generation(self):
        attempt = self.pending()
        self.seal(attempt)
        changed = deepcopy(self.identity)
        changed["target"]["native_epoch"] = "replacement-native-owner"
        changed["target"]["pid"] += 1
        with self.assertRaises(LedgerError):
            self.submit(attempt, self.turn_key(self.identity, 2), identity=changed)
        with self.assertRaises(LedgerError):
            self.confirm(attempt, evidence={**self.evidence(attempt), "epoch": changed["target"]["native_epoch"]})
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], self.expected)
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertIsNone(self.receipt()["announced_at"])

    def test_transport_acceptance_or_uncertainty_stays_held_without_admission_or_replay(self):
        attempt = self.pending()
        self.seal(attempt)
        for status in ("accepted", "uncertain"):
            self.ledger.set_peer_transport(attempt["token"], status, {"socket_status": status})
            with self.subTest(status=status):
                pending = self.ledger.attempt(attempt["token"])
                self.assertEqual((pending["status"], pending["transport"]), ("pending", status))
                self.assertIsNone(pending["verification"])
                self.assertIsNone(self.receipt()["announced_at"])
                self.assertIsNone(self.claim())
                self.assertEqual(len(self.ledger.attempts(self.actor)), 1)

    def test_possible_write_cannot_be_downgraded_to_rejected_and_late_proof_wins(self):
        attempt = self.pending()
        self.seal(attempt)
        self.ledger.set_peer_transport(attempt["token"], "accepted")
        with self.assertRaises(LedgerError):
            self.ledger.set_peer_transport(attempt["token"], "rejected")
        self.assertEqual(self.ledger.attempt(attempt["token"])["transport"], "accepted")
        self.assertTrue(self.confirm(attempt))
        admitted = self.ledger.attempt(attempt["token"])
        self.assertFalse(self.ledger.set_peer_transport(attempt["token"], "rejected"))
        self.assertEqual(self.ledger.attempt(attempt["token"]), admitted)

    def test_unconfirmed_receipt_scan_excludes_rejected_but_retains_possibly_written_retirement(self):
        attempt = self.pending()
        self.seal(attempt)
        self.ledger.set_peer_transport(attempt["token"], "uncertain")
        self.assertEqual([item["token"] for item in self.ledger.unconfirmed_contexts(self.actor)], [attempt["token"]])
        self.ledger.retire_attempt(attempt["token"], "Operator retired uncertain handoff")
        self.assertEqual([item["token"] for item in self.ledger.unconfirmed_contexts(self.actor)], [attempt["token"]])
        identity = self.native_identity(2)
        actor = self.enroll(identity)
        self.post("never-emitted", actor=actor)
        self.start(identity=identity)
        rejected = self.claim(identity=identity)
        self.ledger.set_peer_transport(rejected["token"], "rejected")
        self.assertIsNone(self.ledger.attempt(rejected["token"])["emitted_at"])
        self.assertEqual(self.ledger.unconfirmed_contexts(actor), [])
        self.assertEqual(self.receipt("never-emitted", actor=actor)["notification_eligible"], 1)

    def test_explicit_rejection_before_or_after_emission_preserves_notification_eligibility(self):
        for number, sealed in enumerate((False, True), 2):
            identity = self.native_identity(number)
            actor = self.enroll(identity)
            self.post("rejected-" + str(number), actor=actor)
            self.start(identity=identity)
            attempt = self.claim(identity=identity)
            if sealed:
                self.seal(attempt)
            self.ledger.set_peer_transport(attempt["token"], "rejected", {"native_status": "denied"})
            with self.subTest(sealed=sealed):
                self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "retired")
                receipt = self.receipt("rejected-" + str(number), actor=actor)
                self.assertEqual(receipt["notification_eligible"], 1)
                self.assertIsNone(receipt["announced_at"])
                replacement = self.claim(identity=identity)
                self.assertIsNotNone(replacement)
                self.assertNotEqual(replacement["token"], attempt["token"])

    def test_parallel_claims_have_one_winner_and_confirm_only_frozen_arrivals(self):
        self.post()
        self.start()
        barrier = threading.Barrier(2)
        def claim():
            ledger = Ledger(self.path, clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                return ledger.claim_peer(self.actor, self.binding, self.expected,
                                         native_identity=self.identity, receipt_request=self.request(),
                                         expected_identity=self.identity)
            finally:
                ledger.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: claim(), range(2)))
        winners = [attempt for attempt in results if attempt is not None]
        self.assertEqual(len(winners), 1)
        attempt = winners[0]
        self.assertEqual(attempt["post_ids"], ["initial"])
        self.post("concurrent")
        self.seal(attempt)
        self.assertTrue(self.confirm(attempt))
        self.assertIsNotNone(self.receipt()["announced_at"])
        self.assertIsNone(self.receipt("concurrent")["announced_at"])
        self.assertEqual(len(self.ledger.attempts(self.actor)), 1)


    def test_seen_peer_keeps_late_proof_and_does_not_reserve_new_arrivals(self):
        first = self.pending()
        self.seal(first)
        self.ledger.set_peer_transport(first["token"], "uncertain")
        snapshot = self.ledger.unseen_notice_tokens(self.actor)
        self.post("later")
        second = self.claim()
        self.assertEqual(second["post_ids"], ["later"])
        self.seal(second)
        self.ledger.set_peer_transport(second["token"], "accepted")
        frozen_second = self.ledger.attempt(second["token"])
        self.ledger.acknowledge_notices(self.actor, snapshot)
        self.assertEqual(self.ledger.attempt(second["token"]), frozen_second)
        self.assertEqual([a["token"] for a in self.ledger.unconfirmed_contexts(self.actor)],
                         sorted([first["token"], second["token"]]))
        self.assertTrue(self.ledger.complete(self.actor, self.expected, self.binding))
        self.assertTrue(self.confirm(first))
        self.assertEqual(self.ledger.attempt(first["token"])["status"], "completed")
        self.assertIsNotNone(self.ledger.attempt(first["token"])["agent_seen_at"])
        self.assertEqual(self.ledger.attempt(second["token"]), frozen_second)
        self.assertIsNone(self.receipt("later")["announced_at"])
        self.assertIsNone(self.ledger.active_turn(self.actor))


if __name__ == "__main__":
    unittest.main()
