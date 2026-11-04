"""Busy context is a reserved handoff, separately corroborated under its original turn."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur.ledger import Ledger, LedgerError


class BusyNotificationTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.path = Path(self.home.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 1000)
        self.identities = {}
        self.cursors = {}
        self.actor = self.enroll(1)

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def enroll(self, number):
        session = f"{number:08d}-1111-4111-8111-111111111111"
        actor = "codex:" + session
        identity = {"provider": "codex", "session_id": session, "parent_known": True,
                    "parent_session_id": None, "target": {"cwd": f"/verified/{number}", "native_epoch": None},
                    "evidence": {"source": "verified native capture"}}
        self.identities[actor] = identity
        self.ledger.enroll(actor, identity, "Agent " + str(number), "user-" + str(number))
        self.ledger.sync_conversations(actor, [
            {"id": "build", "name": "Build review", "kind": "channel", "role": "member"},
            {"id": "api", "name": "API review", "kind": "channel", "role": "member"},
            {"id": "private", "name": "Private work", "kind": "dm", "role": "member"}])
        return actor

    def post(self, post_id, *, actor=None, channel="build", chain="exchange"):
        actor = actor or self.actor
        self.cursors[actor] = self.cursors.get(actor, 0) + 1
        self.ledger.ingest(actor, [{"id": post_id, "channel_id": channel, "user_id": "peer",
                                   "author": "Bea", "root_id": "", "create_at": 1001,
                                   "causal": {"chain_ids": [chain]}}], self.cursors[actor], 1)

    def start(self, *, actor=None, turn="T1", epoch="E1", ledger=None):
        actor = actor or self.actor
        return (ledger or self.ledger).start_turn(actor, epoch, turn, native_identity=self.identities[actor])

    def claim(self, tool="tool-1", *, actor=None, turn="T1", epoch="E1", ledger=None, **kwargs):
        actor = actor or self.actor
        return (ledger or self.ledger).claim_context(actor, epoch, turn, tool,
                                                    native_identity=self.identities[actor], **kwargs)

    def confirm(self, attempt, *, ledger=None):
        return (ledger or self.ledger).confirm_context(
            attempt["token"], attempt["actor"], attempt["epoch"], attempt["expected_turn_key"], attempt["tool_id"],
            verification={"native_record": "accepted:" + attempt["token"]})

    def mark(self, attempt):
        return self.ledger.mark_context_emitted(attempt["token"], attempt["actor"], attempt["epoch"],
                                                attempt["expected_turn_key"], attempt["tool_id"],
                                                {"context": "Exact emitted doorbell"})

    def test_claim_requires_the_actual_recipient_owner_turn_and_tool(self):
        self.post("request")
        self.assertIsNone(self.claim())
        self.start()
        original = self.ledger.active_turn(self.actor)
        self.assertIsNone(self.claim(turn="stale-turn"))
        self.assertIsNone(self.claim(epoch="stale-owner"))
        changed = dict(self.identities[self.actor], target={"cwd": "/wrong-owner", "native_epoch": None})
        self.assertIsNone(self.ledger.claim_context(self.actor, "E1", "T1", "tool", native_identity=changed))
        other = self.enroll(2)
        with self.assertRaisesRegex(LedgerError, "immutable"):
            self.ledger.claim_context(self.actor, "E1", "T1", "tool", native_identity=self.identities[other])
        with self.assertRaisesRegex(LedgerError, "tool ID"):
            self.claim("")
        self.assertEqual(self.ledger.active_turn(self.actor), original)
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_owner_checks_allow_new_capture_evidence_but_not_a_changed_current_route(self):
        self.post("request")
        self.start()
        fresh_evidence = dict(self.identities[self.actor], evidence={"source": "actual PostToolUse"})
        attempt = self.ledger.claim_context(self.actor, "E1", "T1", "tool-1", native_identity=fresh_evidence)
        self.assertIsNotNone(attempt)
        self.confirm(attempt)
        self.post("later")
        different_route = dict(self.identities[self.actor], target={"cwd": "/changed", "native_epoch": None})
        self.ledger.update_identity(self.actor, different_route)
        self.assertIsNone(self.claim("tool-2"))

    def test_queue_claim_prevents_a_competing_context_after_a_new_submit(self):
        self.post("request")
        queue = self.ledger.create_attempt(self.actor, "E1", "queued")
        other = Ledger(self.path)
        try:
            self.start(ledger=other)
            self.assertIsNone(self.claim(ledger=other))
        finally:
            other.close()
        self.assertEqual([a["token"] for a in self.ledger.attempts(self.actor)], [queue["token"]])

    def test_context_claim_stays_held_if_the_turn_ends_before_receipt_confirmation(self):
        self.post("request")
        self.start()
        context = self.claim()
        other = Ledger(self.path)
        try:
            self.assertTrue(other.complete(self.actor, "T1", "E1"))
            self.assertIsNone(other.create_attempt(self.actor, "E1"))
            self.assertEqual(other.attempt(context["token"])["status"], "pending")
            self.assertTrue(self.confirm(context, ledger=other))
            self.assertEqual(other.attempt(context["token"])["status"], "completed")
            self.assertIsNone(other.active_turn(self.actor))
        finally:
            other.close()

    def test_simultaneous_queue_and_context_claims_have_only_one_winner(self):
        self.post("request")
        barrier = threading.Barrier(2)

        def queue_claim():
            ledger = Ledger(self.path)
            try:
                barrier.wait(timeout=5)
                return ledger.create_attempt(self.actor, "E1")
            finally:
                ledger.close()

        def context_claim():
            ledger = Ledger(self.path)
            try:
                barrier.wait(timeout=5)
                self.start(ledger=ledger)
                return self.claim(ledger=ledger)
            finally:
                ledger.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(queue_claim), workers.submit(context_claim)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.ledger.attempts(self.actor)), 1)

    def test_multiple_inputs_preserve_original_turn_and_union_provenance_before_receipt(self):
        self.start()
        original = self.ledger.active_turn(self.actor)
        self.post("first", chain="first-origin")
        first = self.claim(receipt_request={"native": "opaque request"})
        self.assertTrue(self.mark(first))
        early_reply = self.ledger.prepare_outbound(self.actor, {"channel_id": "api", "message": "Before proof"})
        self.assertEqual(early_reply["payload"]["causal"], {"chain_ids": ["first-origin"]})
        self.assertTrue(self.confirm(first))
        self.post("second", channel="api", chain="second-origin")
        self.assertIsNone(self.claim("tool-1"))  # Same native event must never emit again.
        second = self.claim("tool-2")
        late_reply = self.ledger.prepare_outbound(self.actor, {"channel_id": "private", "message": "New context"})
        self.assertEqual(late_reply["payload"]["causal"], {"chain_ids": ["first-origin", "second-origin"]})
        self.assertEqual(self.ledger.outbound(early_reply["op_id"])["payload"]["causal"], {"chain_ids": ["first-origin"]})
        self.confirm(second)
        self.assertEqual(self.ledger.active_turn(self.actor), original)
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 2)
        self.ledger.complete(self.actor, "T1", "E1")
        self.assertTrue(all(a["status"] == "completed" for a in self.ledger.attempts(self.actor)))

    def test_context_can_follow_an_admitted_idle_wake_in_the_same_turn(self):
        self.post("idle-request")
        self.ledger.create_attempt(self.actor, "E1", "idle-wake")
        self.ledger.admit("idle-wake", self.actor, "E1", "T1")
        original = self.ledger.active_turn(self.actor)
        self.post("during-work")
        context = self.claim()
        self.assertIsNotNone(context)
        self.confirm(context)
        self.assertEqual(self.ledger.active_turn(self.actor), original)
        self.assertEqual(original["token"], "idle-wake")
        self.ledger.complete(self.actor, "T1", "E1")
        self.assertEqual(self.ledger.attempt("idle-wake")["status"], "completed")
        self.assertEqual(self.ledger.attempt(context["token"])["status"], "completed")

    def test_claim_rechecks_read_and_mute_without_consuming_anything(self):
        self.start()
        self.post("read-before-hook")
        self.ledger.mark_read(self.actor, ["read-before-hook"])
        self.assertIsNone(self.claim())
        self.ledger.set_mute(self.actor, "private", True)
        self.post("muted-dm", channel="private")
        self.assertIsNone(self.claim())
        self.ledger.set_mute(self.actor, "private", False)
        self.assertIsNone(self.claim())
        self.post("new-dm", channel="private")
        context = self.claim()
        self.assertEqual(context["post_ids"], ["new-dm"])
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["muted-dm", "new-dm"])

    def test_late_read_mute_and_newer_turn_do_not_invalidate_actual_acceptance(self):
        self.start()
        self.post("handed-off")
        context = self.claim()
        self.mark(context)
        self.ledger.mark_read(self.actor, ["handed-off"])
        self.ledger.set_mute(self.actor, "build", True)
        self.post("muted-later")
        self.ledger.complete(self.actor, "T1", "E1")
        self.start(turn="T2")
        newer = self.ledger.active_turn(self.actor)
        self.assertTrue(self.confirm(context))
        self.assertFalse(self.confirm(context))
        self.assertEqual(self.ledger.active_turn(self.actor), newer)
        self.assertEqual(self.ledger.attempt(context["token"])["status"], "completed")
        received = dict(self.ledger.db.execute("SELECT read_at,announced_at FROM receipts WHERE post_id='handed-off'").fetchone())
        self.assertIsNotNone(received["read_at"])
        self.assertIsNotNone(received["announced_at"])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_lost_handoff_record_survives_restart_without_route_change_or_reemission(self):
        self.start()
        self.post("possibly-emitted")
        context = self.claim()
        self.ledger.complete(self.actor, "T1", "E1")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 1000)
        self.assertIsNone(self.ledger.create_attempt(self.actor, "E1"))
        self.start(turn="T2")
        self.assertIsNone(self.claim("different-tool", turn="T2"))
        self.assertIsNone(self.ledger.attempt(context["token"])["emitted_at"])
        self.assertTrue(self.confirm(context))  # Exact proof can settle despite missing producer log.
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "T2")

    def test_interruption_closes_confirmed_inputs_and_holds_unconfirmed_input_for_late_proof(self):
        self.start()
        self.post("confirmed")
        confirmed = self.claim("tool-1")
        self.confirm(confirmed)
        self.post("unconfirmed")
        unconfirmed = self.claim("tool-2")
        self.mark(unconfirmed)
        self.ledger.interrupt(self.actor, "T1", "E1")
        self.assertEqual(self.ledger.attempt(confirmed["token"])["status"], "interrupted")
        self.assertEqual(self.ledger.attempt(unconfirmed["token"])["status"], "pending")
        self.start(turn="T2")
        self.assertFalse(self.ledger.complete(self.actor, "T1", "E1"))
        self.confirm(unconfirmed)
        self.assertEqual(self.ledger.attempt(unconfirmed["token"])["status"], "interrupted")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "T2")

    def test_many_confirmed_contexts_preserve_one_original_turn_without_a_cap(self):
        self.start()
        original = self.ledger.active_turn(self.actor)
        for number in range(20):
            self.post("confirmed-" + str(number), channel="build" if number % 2 else "api")
            context = self.claim("tool-" + str(number))
            self.assertIsNotNone(context)
            self.assertTrue(self.confirm(context))
            self.assertEqual(self.ledger.active_turn(self.actor), original)
        self.assertEqual(len(self.ledger.attempts(self.actor)), 20)
        self.assertTrue(self.ledger.complete(self.actor, "T1", "E1"))
        self.assertTrue(all(a["status"] == "completed" for a in self.ledger.attempts(self.actor)))

    def test_retiring_uncertain_inputs_never_caps_new_work_or_replays_frozen_arrivals(self):
        self.start()
        first = None
        for number in range(20):
            self.post("uncertain-" + str(number))
            context = self.claim("tool-" + str(number))
            self.assertIsNotNone(context)
            first = first or context
            self.ledger.retire_attempt(context["token"], "Uncertain native output retired by operator")
            self.assertIsNone(self.claim("retry-" + str(number)))
        self.post("later", channel="api", chain="unrelated-room")
        later = self.claim("later-tool")
        self.assertEqual(later["post_ids"], ["later"])
        self.assertTrue(self.confirm(later))
        self.assertTrue(self.confirm(first))
        self.assertFalse(self.confirm(first))
        self.assertEqual(len(self.ledger.unread_posts(self.actor, limit=1000)), 21)
        self.assertEqual(sum(p["announced_at"] is not None for p in self.ledger.unread_posts(self.actor, limit=1000)), 2)

    def test_legacy_exhausted_state_cannot_block_idle_or_late_context_receipts(self):
        self.start()
        self.post("pending-context")
        context = self.claim()
        self.ledger.db.execute("CREATE TABLE chains(id TEXT PRIMARY KEY, admissions INTEGER NOT NULL)")
        self.ledger.db.execute("INSERT INTO chains VALUES('exchange', 999)")
        self.ledger.db.execute("ALTER TABLE attempts ADD COLUMN budget_state TEXT NOT NULL DEFAULT 'reserved'")
        self.ledger.complete(self.actor, "T1", "E1")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 1000)
        # Unknown output remains pending, irrespective of the former cap.
        self.assertIsNone(self.ledger.create_attempt(self.actor, "E1"))
        self.ledger.retire_attempt(context["token"], "Resolve uncertain old handoff")
        self.assertTrue(self.confirm(context))
        self.assertFalse(self.confirm(context))
        self.assertEqual(self.ledger.attempt(context["token"])["status"], "completed")
        other = self.enroll(2)
        self.post("idle-other", actor=other)
        idle = self.ledger.create_attempt(other, "E2", "later-idle")
        self.assertTrue(self.ledger.admit(idle["token"], other, "E2", "T2"))
        self.assertEqual(self.ledger.db.execute("SELECT admissions FROM chains").fetchone()[0], 999)
        self.assertEqual(self.ledger.db.execute("SELECT budget_state FROM attempts WHERE token=?", (context["token"],)).fetchone()[0], "reserved")

    def test_different_recipients_with_shared_origin_can_claim_concurrently(self):
        other = self.enroll(2)
        self.start()
        self.start(actor=other, turn="T2", epoch="E2")
        self.post("a")
        self.post("b", actor=other)
        barrier = threading.Barrier(2)

        def claim(actor, epoch, turn):
            ledger = Ledger(self.path)
            try:
                barrier.wait(timeout=5)
                return self.claim("tool", actor=actor, epoch=epoch, turn=turn, ledger=ledger)
            finally:
                ledger.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(claim, self.actor, "E1", "T1"), workers.submit(claim, other, "E2", "T2")]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual({a["actor"] for a in results}, {self.actor, other})
        for attempt in results:
            self.assertTrue(self.confirm(attempt))

    def test_receipts_bind_all_native_ids_and_require_bounded_positive_evidence(self):
        self.start()
        self.post("request")
        with self.assertRaisesRegex(LedgerError, "64 KiB"):
            self.claim(receipt_request={"huge": "x" * 65536})
        context = self.claim(receipt_request={"native": {"format": "opaque"}})
        token = context["token"]
        for actor, epoch, turn, tool in (("wrong-recipient", "E1", "T1", "tool-1"),
                                         (self.actor, "wrong-owner", "T1", "tool-1"),
                                         (self.actor, "E1", "old-turn", "tool-1"),
                                         (self.actor, "E1", "T1", "wrong-tool")):
            with self.assertRaisesRegex(LedgerError, "does not match"):
                self.ledger.confirm_context(token, actor, epoch, turn, tool, verification={"proof": True})
        with self.assertRaises(LedgerError):
            self.ledger.confirm_context(token, self.actor, "E1", "T1", "tool-1", verification={})
        with self.assertRaisesRegex(LedgerError, "64 KiB"):
            self.ledger.confirm_context(token, self.actor, "E1", "T1", "tool-1", verification={"huge": "x" * 65536})
        with self.assertRaisesRegex(LedgerError, "corroborated receipt"):
            self.ledger.admit(token, self.actor, "E1", "T1")
        with self.assertRaisesRegex(LedgerError, "handoff and receipt"):
            self.ledger.set_transport(token, "accepted")
        self.assertTrue(self.mark(context))
        self.assertFalse(self.mark(context))
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.confirm(context)
        self.assertFalse(self.mark(context))
        self.assertFalse(self.confirm(context))

    def test_old_sqlite_attempts_and_indexes_migrate_without_losing_idle_state(self):
        self.post("legacy-post")
        self.ledger.create_attempt(self.actor, "E1", "legacy-token")
        self.ledger.admit("legacy-token", self.actor, "E1", "T1")
        original_turn = self.ledger.active_turn(self.actor)
        self.ledger.close()
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript("""
                DROP INDEX IF EXISTS one_open_attempt;
                DROP INDEX native_attempt_turn;
                DROP INDEX context_tool_once;
                ALTER TABLE attempts RENAME TO busy_attempts;
                CREATE TABLE attempts (
                    token TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES actors(actor),
                    epoch TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    transport TEXT NOT NULL DEFAULT 'pending', evidence_json TEXT,
                    post_ids_json TEXT NOT NULL, channels_json TEXT NOT NULL,
                    causal_json TEXT NOT NULL, turn_key TEXT,
                    created_at INTEGER NOT NULL, admitted_at INTEGER, completed_at INTEGER, reason TEXT
                );
                INSERT INTO attempts SELECT token,actor,epoch,status,transport,evidence_json,
                    post_ids_json,channels_json,causal_json,turn_key,created_at,admitted_at,completed_at,reason
                    FROM busy_attempts;
                DROP TABLE busy_attempts;
                CREATE UNIQUE INDEX one_open_attempt ON attempts(actor) WHERE status IN ('pending','admitted');
                CREATE UNIQUE INDEX native_attempt_turn ON attempts(actor,epoch,turn_key) WHERE turn_key IS NOT NULL;
            """)
        finally:
            connection.close()
        self.ledger = Ledger(self.path, clock=lambda: 1000)
        old = self.ledger.attempt("legacy-token")
        self.assertEqual((old["mode"], old["status"], old["turn_key"]), ("idle", "admitted", "T1"))
        self.assertEqual(self.ledger.active_turn(self.actor), original_turn)
        self.post("after-upgrade")
        context = self.claim()
        self.assertIsNotNone(context)
        self.confirm(context)
        self.ledger.complete(self.actor, "T1", "E1")
        self.assertEqual(self.ledger.attempt("legacy-token")["status"], "completed")
        self.assertEqual(self.ledger.attempt(context["token"])["status"], "completed")


    def test_unconfirmed_batch_reserves_only_its_posts_and_later_batch_survives_reopen(self):
        self.post("A")
        self.start()
        first = self.claim("tool-A")
        self.mark(first)
        self.post("B")
        second = self.claim("tool-B")
        self.assertEqual((first["post_ids"], second["post_ids"]), (["A"], ["B"]))
        self.mark(second)
        for tool in ("tool-A", "tool-B", "unrelated-next-tool"):
            self.assertIsNone(self.claim(tool))
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])
        self.assertEqual([p["id"] for p in self.ledger._unread(self.actor, eligible=True)], ["A", "B"])
        frozen_second = self.ledger.attempt(second["token"])
        before_receipts = [tuple(r) for r in self.ledger.db.execute("SELECT * FROM receipts ORDER BY arrival")]
        original_turn = self.ledger.active_turn(self.actor)
        acknowledged = self.ledger.acknowledge_notices(self.actor, [first["token"]])
        self.assertFalse(acknowledged[0]["already_seen"])
        self.assertEqual(self.ledger.active_turn(self.actor), original_turn)
        self.assertEqual([tuple(r) for r in self.ledger.db.execute("SELECT * FROM receipts ORDER BY arrival")], before_receipts)
        self.assertEqual(self.ledger.attempt(second["token"]), frozen_second)
        self.ledger.complete(self.actor, "T1", "E1")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 2000)
        self.assertTrue(self.ledger.acknowledge_notices(self.actor, [first["token"]])[0]["already_seen"])
        self.assertEqual(self.ledger.attempt(first["token"])["agent_seen_at"], 1000)
        self.assertIsNone(self.ledger.create_attempt(self.actor, "E1"))
        self.assertTrue(self.confirm(first))  # Late proof records the original completed turn.
        self.assertEqual(self.ledger.attempt(first["token"])["status"], "completed")
        self.assertEqual(self.ledger.attempt(second["token"])["status"], "pending")
        self.assertIsNone(self.ledger.attempt(second["token"])["agent_seen_at"])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[1]["announced_at"])
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_idle_receipt_snapshot_does_not_consume_or_acknowledge_a_concurrent_batch(self):
        self.post("A")
        first = self.ledger.create_attempt(self.actor, "E1", "idle-A")
        self.ledger.set_transport(first["token"], "uncertain")
        snapshot = self.ledger.unseen_notice_tokens(self.actor)
        self.post("B")
        second = self.ledger.create_attempt(self.actor, "E1", "idle-B")
        self.assertEqual(second["post_ids"], ["B"])
        self.ledger.set_transport(second["token"], "accepted")
        first_before = self.ledger.attempt(first["token"])
        second_before = self.ledger.attempt(second["token"])
        self.assertEqual(self.ledger.acknowledge_notices(self.actor, snapshot), [
            {"token": "idle-A", "seen_at": 1000, "already_seen": False}])
        expected_first = dict(first_before, agent_seen_at=1000)
        self.assertEqual(self.ledger.attempt(first["token"]), expected_first)
        self.assertEqual(self.ledger.attempt(second["token"]), second_before)
        self.assertEqual(self.ledger.unseen_notice_tokens(self.actor), ["idle-B"])
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 2)
        self.assertTrue(all(p["announced_at"] is None for p in self.ledger.unread_posts(self.actor)))
        self.ledger.admit("idle-A", self.actor, "E1", "native-A")
        self.ledger.complete(self.actor, "native-A", "E1")
        self.ledger.admit("idle-B", self.actor, "E1", "native-B")
        self.assertEqual(self.ledger.active_turn(self.actor)["token"], "idle-B")

    def test_receipt_validation_is_atomic_and_rejection_after_snapshot_preserves_awareness(self):
        self.post("A")
        first = self.ledger.create_attempt(self.actor, "E1", "own")
        other = self.enroll(2)
        self.post("other", actor=other)
        other_attempt = self.ledger.create_attempt(other, "E2", "foreign")
        before = deepcopy(self.ledger.attempts())
        with self.assertRaisesRegex(LedgerError, "another recipient"):
            self.ledger.acknowledge_notices(self.actor, [first["token"], other_attempt["token"]])
        self.assertEqual(self.ledger.attempts(), before)
        with self.assertRaisesRegex(LedgerError, "Unknown notification"):
            self.ledger.acknowledge_notices(self.actor, [first["token"], "missing"])
        self.assertEqual(self.ledger.attempts(), before)
        for tokens in ("own", [" bad token "], [None]):
            with self.assertRaises(LedgerError):
                self.ledger.acknowledge_notices(self.actor, tokens)
        snapshot = self.ledger.unseen_notice_tokens(self.actor)
        self.ledger.set_transport(first["token"], "rejected")
        rejected = self.ledger.attempt(first["token"])
        receipts = [tuple(row) for row in self.ledger.db.execute("SELECT * FROM receipts ORDER BY arrival")]
        self.assertEqual(self.ledger.unseen_notice_tokens(self.actor), [])
        self.assertEqual(self.ledger.acknowledge_notices(self.actor, snapshot), [
            {"token": first["token"], "seen_at": 1000, "already_seen": False}])
        self.assertEqual(self.ledger.acknowledge_notices(self.actor, snapshot), [
            {"token": first["token"], "seen_at": 1000, "already_seen": True}])
        self.assertEqual(self.ledger.attempt(first["token"]), dict(rejected, agent_seen_at=1000))
        self.assertEqual([tuple(row) for row in self.ledger.db.execute("SELECT * FROM receipts ORDER BY arrival")], receipts)
        self.assertEqual(self.ledger.notification_candidates(self.actor)[0]["id"], "A")
        # Awareness does not fabricate a handoff: positive prewrite rejection
        # still allows this same cohort its first physical delivery.
        offered = self.ledger.create_attempt(self.actor, "E1", "replacement")
        self.assertEqual(offered["post_ids"], ["A"])
        self.assertIsNone(self.ledger.attempt(offered["token"])["agent_seen_at"])
        self.assertIsNone(self.ledger.create_attempt(self.actor, "E1", "another-copy"))

    def test_compact_attention_stays_quiet_for_muted_reference_and_unenrolled_sessions(self):
        self.assertFalse(self.ledger.has_pending_attention("not-enrolled"))
        self.assertFalse(self.ledger.has_pending_attention(self.actor))
        self.post("request")
        self.assertTrue(self.ledger.has_pending_attention(self.actor))
        attempt = self.ledger.create_attempt(self.actor, "E1", "attention")
        self.ledger.mark_read(self.actor, ["request"])
        self.assertTrue(self.ledger.has_pending_attention(self.actor))  # Unacknowledged exact notice.
        self.ledger.set_mute(self.actor, "build", True)
        self.assertFalse(self.ledger.has_pending_attention(self.actor))
        self.ledger.set_mute(self.actor, "build", False)
        # A mute preference suppresses the old batch even after unmuting.
        self.assertFalse(self.ledger.has_pending_attention(self.actor))
        self.ledger.acknowledge_notices(self.actor, [attempt["token"]])
        self.assertFalse(self.ledger.has_pending_attention(self.actor))
        self.ledger.sync_conversations(self.actor, [
            {"id": "build", "name": "Reference", "kind": "channel", "role": "observer"}])
        self.post("reference")
        self.assertFalse(self.ledger.has_pending_attention(self.actor))

    def test_parallel_new_batch_claims_reserve_one_cohort_without_actor_global_lock(self):
        self.post("A")
        self.start()
        old = self.claim("tool-A")
        self.mark(old)
        self.post("B")
        barrier = threading.Barrier(2)

        def take(tool):
            ledger = Ledger(self.path)
            try:
                barrier.wait(timeout=5)
                return self.claim(tool, ledger=ledger)
            finally:
                ledger.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(take, ("next-1", "next-2")))
        winners = [attempt for attempt in results if attempt]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["post_ids"], ["B"])
        self.assertEqual(len(self.ledger.pending_attempts(self.actor)), 2)


    def test_out_of_order_idle_admission_unions_unresolved_provenance_without_rewriting_seals(self):
        identity = self.identities[self.actor]

        def freeze(post, chain, token):
            self.post(post, chain=chain)
            attempt = self.ledger.create_attempt(self.actor, "E1", token,
                expected_identity=identity, refreshed_identity=identity)
            account = self.ledger.actor(self.actor)
            enrollment = {key: account[key] for key in ("enrollment_id", "enrollment_seq", "user_id")}
            emission = {"schema": 1, "kind": "codex-idle-handoff-v1", "actor": self.actor,
                        "session_id": identity["session_id"], "token": token,
                        "text": "You have new messages. [wake:" + token + "]",
                        "gate": {"ledger": enrollment}}
            self.assertTrue(self.ledger.seal_idle_handoff(token, self.actor, "E1", emission,
                                                         expected_enrollment=enrollment))
            self.ledger.set_transport(token, "uncertain", {"queue_id": "queued-" + token})
            return self.ledger.attempt(token)

        first = freeze("A", "origin-A", "notice-A")
        second = freeze("B", "origin-B", "notice-B")
        self.assertEqual((first["causal"], second["causal"]),
                         ({"chain_ids": ["origin-A"]}, {"chain_ids": ["origin-B"]}))
        self.ledger.admit("notice-B", self.actor, "E1", "native-B", native_identity=identity)
        early = self.ledger.prepare_outbound(self.actor, {"message": "B reply"})
        self.assertEqual(early["payload"]["causal"], {"chain_ids": ["origin-B"]})
        self.ledger.admit("notice-A", self.actor, "E1", "native-A", supersede=True, native_identity=identity)
        self.assertEqual(self.ledger.attempt("notice-B")["status"], "interrupted")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "native-A")
        combined = {"chain_ids": ["origin-A", "origin-B"]}
        self.assertEqual(self.ledger.causal_context(self.actor), combined)
        later = self.ledger.prepare_outbound(self.actor,
            {"message": "Reply after delayed A", "channel_id": "api", "root_id": "another-thread"})
        self.assertEqual(later["payload"]["causal"], combined)
        self.assertEqual(self.ledger.outbound(early["op_id"])["payload"]["causal"], {"chain_ids": ["origin-B"]})
        frozen_keys = ("post_ids", "channels", "causal", "native_identity", "receipt_request", "emission",
                       "emitted_at", "epoch", "expected_turn_key", "tool_id")
        for original in (first, second):
            current = self.ledger.attempt(original["token"])
            self.assertEqual({key: current[key] for key in frozen_keys},
                             {key: original[key] for key in frozen_keys})


if __name__ == "__main__":
    unittest.main()
