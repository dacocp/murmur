"""Real SQLite indexing and scoped native proofs; no native clients or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from murmur import evidence
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity, lineage
from murmur.native.types import IdentityError


PROCESS = {"pid": 2300, "proc_start": "fixture birth", "entrypoint": "cli",
           "version": "2.1.310", "root_start_verified": True}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ledger = Ledger(self.root / "operations.sqlite3")
        self.addCleanup(self.ledger.close)
        evidence.initialize(self.ledger.db)
        self.session, self.child, self.grandchild = (str(uuid.uuid4()) for _ in range(3))
        self.identity = NativeIdentity("claude", self.session, target={**PROCESS, "runtime": "cli"})

    def capture(self, session=None, process=None, event="SessionStart", **fields):
        return lineage.observation({"hook_event_name": event, "session_id": session or self.session,
                                    **fields}, process or PROCESS)

    def raw(self, value):
        self.ledger.db.execute("INSERT INTO native_observations(evidence,captured_at) VALUES(?,100)",
                               (json.dumps(value, sort_keys=True),))

    def certify(self, identity=None):
        identity = identity or self.identity
        selected = evidence.lineage_evidence(self.ledger, identity)
        return lineage.certify(identity.session_id, config_dir=str(self.root), process=identity.target, **selected)

    def root_proof(self):
        observed = self.capture(source="startup")
        proof = lineage.certify(self.session, observations=[observed], config_dir=str(self.root), process=PROCESS)
        evidence.persist_proofs(self.ledger, proof["ancestors"])
        return proof

    def branch(self, parent, child, *, prompt=None):
        prompt = prompt or str(uuid.uuid4())
        path = self.root / "projects/work" / (child + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "sessionId": child,
                                    "forkedFrom": {"sessionId": parent}}) + "\n")
        path.chmod(0o600)
        return [self.capture(parent, event="SessionEnd", reason="resume", prompt_id=prompt),
                self.capture(child, source="fork", prompt_id=prompt, transcript_path=str(path))]

    def test_legacy_migration_scopes_more_than_4096_unrelated_same_process_records(self):
        unrelated = [self.capture(str(uuid.uuid4()), source="startup") for _ in range(4100)]
        with self.ledger._write():
            self.ledger.db.executemany("INSERT INTO native_observations(evidence,captured_at) VALUES(?,100)",
                                      [(json.dumps(row, sort_keys=True),) for row in unrelated])
            self.raw(self.capture(source="startup"))
        before = list(self.ledger.db.execute("SELECT id,evidence,captured_at FROM native_observations ORDER BY id"))
        proof = self.certify()
        self.assertEqual(proof["session_id"], self.session)
        self.assertIsNone(proof["parent_session_id"])
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in self.ledger.db.execute(
            "SELECT id,evidence,captured_at FROM native_observations ORDER BY id")])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations WHERE indexed=0").fetchone()[0], 0)
        self.assertEqual(len(evidence.lineage_evidence(self.ledger, self.identity)["observations"]), 1)

    def test_original_schema_is_migrated_without_changing_raw_certificates_or_observations(self):
        legacy = Ledger(self.root / "legacy.sqlite3")
        self.addCleanup(legacy.close)
        legacy.db.executescript("""
            CREATE TABLE native_observations(id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence TEXT NOT NULL UNIQUE,captured_at INTEGER NOT NULL);
            CREATE TABLE native_proofs(actor TEXT PRIMARY KEY,evidence TEXT NOT NULL);
        """)
        observed = self.capture(source="startup")
        proof = lineage.certify(self.session, observations=[observed], config_dir=str(self.root), process=PROCESS)
        raw_capture, raw_proof = json.dumps(observed, sort_keys=True), json.dumps(proof["ancestors"][0], sort_keys=True)
        legacy.db.execute("INSERT INTO native_observations(evidence,captured_at) VALUES(?,100)", (raw_capture,))
        legacy.db.execute("INSERT INTO native_proofs VALUES(?,?)", (self.identity.actor, raw_proof))
        selected = evidence.lineage_evidence(legacy, self.identity)
        self.assertEqual(selected, {"observations": [observed], "known": proof["ancestors"]})
        self.assertEqual(legacy.db.execute("SELECT evidence FROM native_observations").fetchone()[0], raw_capture)
        self.assertEqual(legacy.db.execute("SELECT evidence FROM native_proofs").fetchone()[0], raw_proof)

    def test_hook_append_never_indexes_or_scans_legacy_history(self):
        self.raw(self.capture(self.child, source="startup"))
        with patch.object(evidence, "_migrate", side_effect=AssertionError("Hook migrated history")), \
                patch.object(evidence, "_indexes", side_effect=AssertionError("Hook indexed history")):
            evidence.initialize(self.ledger.db)
            evidence.record_observation(self.ledger, self.capture(source="startup"))
        self.assertEqual([row[0] for row in self.ledger.db.execute("SELECT indexed FROM native_observations ORDER BY id")], [0, 1])

    def test_desktop_bypasses_cli_indexing_even_with_unrelated_corrupt_legacy_rows(self):
        self.ledger.db.execute("INSERT INTO native_observations(evidence,captured_at) VALUES('not json',100)")
        identity = NativeIdentity("claude", "local_" + self.session, target={"runtime": "desktop"})
        with patch.object(evidence, "_migrate", side_effect=AssertionError("Desktop read CLI evidence")):
            self.assertEqual(evidence.lineage_evidence(self.ledger, identity), {"observations": [], "known": []})
        self.assertEqual(self.ledger.db.execute("SELECT indexed FROM native_observations").fetchone()[0], 0)

    def test_required_failed_capture_remains_visible_after_certification_and_resume(self):
        proof = self.root_proof()
        failed = self.capture(source="startup")
        failed["event_key"] = "changed"
        self.raw(failed)
        resumed = NativeIdentity("claude", self.session, target={**PROCESS, "pid": 4800,
                                 "proc_start": "new birth", "root_start_verified": False, "runtime": "cli"})
        with self.assertRaisesRegex(IdentityError, "immutable observation changed"):
            self.certify(resumed)
        self.assertEqual(len(evidence.lineage_evidence(self.ledger, resumed)["known"]), 1)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations").fetchone()[0], 1)
        self.assertEqual(proof["session_id"], self.session)

    def test_failed_scope_keeps_valid_process_when_session_address_was_corrupted(self):
        self.raw(self.capture(source="startup"))
        failed = self.capture(self.child, source="startup")
        failed["payload"]["session_id"] = "broken"
        self.raw(failed)
        selected = evidence.lineage_evidence(self.ledger, self.identity)
        self.assertEqual(len(selected["observations"]), 2)
        with self.assertRaisesRegex(IdentityError, "creation proof unresolved"):
            self.certify()

    def test_unrelated_other_process_failure_does_not_block_target(self):
        self.raw(self.capture(source="startup"))
        failed = self.capture(self.child, process={**PROCESS, "pid": 4800}, source="startup")
        failed["event_key"] = "changed"
        self.raw(failed)
        self.assertEqual(self.certify()["session_id"], self.session)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations WHERE index_error IS NOT NULL").fetchone()[0], 1)

    def test_unaddressable_failed_capture_holds_cli_without_guessing_it_was_unrelated(self):
        self.raw(self.capture(source="startup"))
        failed = self.capture(self.child, source="startup")
        failed["payload"]["session_id"], failed["process"]["pid"] = "broken", "broken"
        self.raw(failed)
        with self.assertRaisesRegex(LedgerError, "unaddressable failed capture"):
            self.certify()
        row = self.ledger.db.execute("""SELECT session_id,process_key,index_error FROM native_observations
            WHERE index_error IS NOT NULL""").fetchone()
        self.assertEqual(tuple(row[:2]), (None, None))
        self.assertTrue(row[2])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations").fetchone()[0], 2)

    def test_branch_closure_certifies_unenrolled_intermediate_from_required_transitions(self):
        self.root_proof()
        for observed in [*self.branch(self.session, self.child), *self.branch(self.child, self.grandchild)]:
            evidence.record_observation(self.ledger, observed)
        identity = NativeIdentity("claude", self.grandchild, target={**PROCESS, "runtime": "cli"})
        proof = self.certify(identity)
        self.assertEqual([row["session_id"] for row in proof["ancestors"]],
                         [self.session, self.child, self.grandchild])
        self.assertEqual(self.ledger.actors(), [])

    def test_consumed_transition_outside_target_closure_cannot_be_reused(self):
        root = self.root_proof()
        prompt = str(uuid.uuid4())
        first = self.branch(self.session, self.child, prompt=prompt)
        consumed = lineage.certify(self.child, observations=first, known=root["ancestors"],
                                   config_dir=str(self.root), process=PROCESS)
        evidence.persist_proofs(self.ledger, consumed["ancestors"])
        for observed in self.branch(self.session, self.grandchild, prompt=prompt):
            evidence.record_observation(self.ledger, observed)
        identity = NativeIdentity("claude", self.grandchild, target={**PROCESS, "runtime": "cli"})
        with self.assertRaisesRegex(IdentityError, "consumed"):
            self.certify(identity)

    def test_certificates_remain_immutable_and_legacy_proofs_are_indexed(self):
        proof = self.root_proof()["ancestors"][0]
        self.ledger.db.execute("UPDATE native_proofs SET indexed=0,process_key=NULL")
        selected = evidence.lineage_evidence(self.ledger, self.identity)
        self.assertEqual(selected["known"], [proof])
        changed = {**proof, "proc_start": "different proof"}
        with self.assertRaisesRegex(LedgerError, "conflicts"):
            evidence.persist_proofs(self.ledger, [changed])
        self.assertEqual(json.loads(self.ledger.db.execute("SELECT evidence FROM native_proofs").fetchone()[0]), proof)

    def test_persistence_rejects_a_transition_consumed_after_a_stale_proof_read(self):
        root = self.root_proof()
        prompt = str(uuid.uuid4())
        certificates = [lineage.certify(child, observations=self.branch(self.session, child, prompt=prompt),
            known=root["ancestors"], config_dir=str(self.root), process=PROCESS)
            for child in (self.child, self.grandchild)]
        evidence.persist_proofs(self.ledger, certificates[0]["ancestors"])
        with self.assertRaisesRegex(LedgerError, "already consumed"):
            evidence.persist_proofs(self.ledger, certificates[1]["ancestors"])
        self.assertIsNone(self.ledger.db.execute("SELECT actor FROM native_proofs WHERE actor=?",
                                               ("claude:" + self.grandchild,)).fetchone())

    def test_per_proof_bound_and_resumable_migration_preserve_audit_rows(self):
        for observed in (self.capture(source="startup"), self.capture(source="startup", prompt_id=str(uuid.uuid4()))):
            self.raw(observed)
        with patch.object(evidence, "_BATCH", 1), \
                patch.object(evidence.time, "monotonic", side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(LedgerError, "indexing is pending"):
                evidence.lineage_evidence(self.ledger, self.identity, timeout=3)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations WHERE indexed=1").fetchone()[0], 1)
        with patch.object(lineage, "MAX_OBSERVATIONS", 1):
            with self.assertRaisesRegex(LedgerError, "Relevant native creation evidence exceeds"):
                evidence.lineage_evidence(self.ledger, self.identity)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM native_observations").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
