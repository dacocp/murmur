"""Pure lineage proof tests with real bounded transcript metadata fixtures."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from murmur.native import lineage
from murmur.native.types import IdentityError


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name).resolve()
        self.project = self.config / "projects/fixture"
        self.project.mkdir(parents=True)
        self.a, self.b, self.c = [str(uuid.uuid4()) for _ in range(3)]
        self.process = dict(pid=2300, proc_start="native birth", entrypoint="cli",
                            version="2.1.310", root_start_verified=True)

    def capture(self, event="SessionStart", session=None, **fields):
        return lineage.observation(dict(hook_event_name=event, session_id=session or self.a,
                                        **fields), self.process)

    def certify(self, session, observations=(), known=(), process=None):
        return lineage.certify(session, observations=observations, known=known,
                               config_dir=self.config, process=process or self.process)

    def root(self):
        return self.certify(self.a, [self.capture(source="startup")])

    def branch(self, parent, child, *, metadata_parent=None, prompt=None):
        prompt = prompt or str(uuid.uuid4())
        path = self.project / f"{child}.jsonl"
        path.write_text(json.dumps(dict(type="user", sessionId=child,
                                        forkedFrom={"sessionId": metadata_parent or parent})) + "\n")
        return [self.capture("SessionEnd", parent, reason="resume", prompt_id=prompt),
                self.capture(session=child, source="fork", prompt_id=prompt,
                             transcript_path=str(path))]

    def test_capture_is_bounded_immutable_metadata_without_prompt_or_transcript_body(self):
        observed = self.capture(source="startup", prompt="secret", arbitrary={"secret": True})
        encoded = json.dumps(observed)
        self.assertNotIn("secret", encoded)
        self.assertEqual(observed, self.capture(source="startup"))
        changed = copy.deepcopy(observed)
        changed["process"]["root_start_verified"] = False
        with self.assertRaisesRegex(IdentityError, "immutable observation"):
            self.certify(self.a, [changed])

    def test_startup_requires_pinned_profile_uuid4_and_same_current_engine(self):
        observation = self.capture(source="startup")
        self.assertIsNone(self.certify(self.a, [observation])["parent_session_id"])
        with self.assertRaises(IdentityError):
            self.certify(self.b, [observation])
        process = {**self.process, "root_start_verified": False}
        with self.assertRaisesRegex(IdentityError, "plain startup"):
            self.certify(self.a, [observation], process=process)
        remote = str(uuid.uuid5(uuid.NAMESPACE_URL, "remote initializer"))
        with self.assertRaises(IdentityError):
            self.certify(remote, [self.capture(session=remote, source="startup")])
        for version in (None, "2.1.311"):
            with self.subTest(version=version), self.assertRaises(IdentityError):
                lineage.observation({"hook_event_name": "SessionStart", "session_id": self.a,
                                     "source": "startup"}, {**self.process, "version": version})

    def test_branch_chain_uses_direct_transition_parent_and_complete_known_ancestry(self):
        a = self.root()
        b = self.certify(self.b, self.branch(self.a, self.b), known=a["ancestors"])
        c = self.certify(self.c, self.branch(self.b, self.c), known=b["ancestors"])
        self.assertEqual(b["parent_session_id"], self.a)
        self.assertEqual(c["parent_session_id"], self.b)
        self.assertEqual([item["session_id"] for item in c["ancestors"]], [self.a, self.b, self.c])
        self.assertEqual(c["root_session_id"], self.a)
        self.assertEqual(json.loads(json.dumps(c)), c)
        with self.assertRaisesRegex(IdentityError, "complete certified ancestry"):
            self.certify(self.c, known=[c["ancestors"][-1]])

    def test_stale_family_root_in_child_transcript_never_replaces_immediate_parent(self):
        a = self.root()
        b = self.certify(self.b, self.branch(self.a, self.b), known=a["ancestors"])
        with self.assertRaisesRegex(IdentityError, "transcript proof"):
            self.certify(self.c, self.branch(self.b, self.c, metadata_parent=self.a),
                         known=b["ancestors"])

    def test_end_and_start_need_same_prompt_pid_birth_and_cannot_reuse_transition(self):
        known = self.root()["ancestors"]
        observations = self.branch(self.a, self.b)
        mismatch = copy.deepcopy(observations)
        mismatch[1] = self.capture(session=self.b, source="fork", prompt_id=str(uuid.uuid4()),
                                  transcript_path=observations[1]["payload"]["transcript_path"])
        with self.assertRaises(IdentityError):
            self.certify(self.b, mismatch, known)
        changed = {**self.process, "proc_start": "new birth"}
        with self.assertRaises(IdentityError):
            self.certify(self.b, observations, known, changed)
        same_prompt = observations[0]["payload"]["prompt_id"]
        other_child = self.branch(self.a, self.c, prompt=same_prompt)[1]
        with self.assertRaises(IdentityError):
            self.certify(self.c, [*observations, other_child], known)

    def test_known_cold_resume_keeps_lineage_but_unknown_resume_never_becomes_root(self):
        a = self.root()
        b = self.certify(self.b, self.branch(self.a, self.b), a["ancestors"])
        new = {**self.process, "pid": 4800, "proc_start": "later birth", "root_start_verified": False}
        observation = lineage.observation({"hook_event_name": "SessionStart", "session_id": self.b,
                                          "source": "resume"}, new)
        resumed = self.certify(self.b, [observation], b["ancestors"], new)
        self.assertEqual(resumed["parent_session_id"], self.a)
        self.assertEqual(resumed["ancestors"], b["ancestors"])
        with self.assertRaises(IdentityError):
            self.certify(self.b, [observation], process=new)
        # An unrelated failed resume cannot destroy known valid ancestry.
        unrelated = lineage.observation({"hook_event_name": "SessionStart", "session_id": self.c,
                                        "source": "resume"}, new)
        self.assertEqual(self.certify(self.b, [unrelated], b["ancestors"], new)["parent_session_id"], self.a)

    def test_old_target_or_ancestor_failure_survives_cold_resume_but_unrelated_failure_does_not(self):
        a = self.root()
        b = self.certify(self.b, self.branch(self.a, self.b), a["ancestors"])
        new = {**self.process, "pid": 4800, "proc_start": "new birth", "root_start_verified": False}
        for session in (self.a, self.b):
            conflict = self.capture(session=session, source="fork", prompt_id=str(uuid.uuid4()))
            with self.subTest(session=session), self.assertRaisesRegex(IdentityError, "preceding native SessionEnd"):
                self.certify(self.b, [conflict], b["ancestors"], new)
        unrelated = self.capture(session=self.c, source="fork", prompt_id=str(uuid.uuid4()))
        self.assertEqual(self.certify(self.b, [unrelated], b["ancestors"], new)["parent_session_id"], self.a)

    def test_certified_creation_replay_never_rescans_transcript_and_consumption_is_durable(self):
        a = self.root()
        observations = self.branch(self.a, self.b)
        b = self.certify(self.b, observations, a["ancestors"])
        Path(observations[-1]["payload"]["transcript_path"]).unlink()
        new = {**self.process, "pid": 4800, "proc_start": "new birth", "root_start_verified": False}
        with patch.object(lineage, "transcript_proof", side_effect=AssertionError("rescanned")):
            replayed = self.certify(self.b, observations, b["ancestors"], new)
        self.assertEqual(replayed["ancestors"], b["ancestors"])
        prompt = observations[0]["payload"]["prompt_id"]
        other = self.branch(self.a, self.c, prompt=prompt)
        with self.assertRaisesRegex(IdentityError, "consumed"):
            self.certify(self.c, other, b["ancestors"])

    def test_transcript_bounds_symlinks_torn_and_duplicate_metadata_are_rejected(self):
        known = self.root()["ancestors"]
        observed = self.branch(self.a, self.b)
        path = Path(observed[-1]["payload"]["transcript_path"])
        original = path.read_bytes()
        for data in (original[:-1], b'{"type":"user","type":"assistant"}\n',
                     json.dumps({"type": "user", "sessionId": self.a}).encode() + b"\n"):
            with self.subTest(data=data):
                path.write_bytes(data)
                with self.assertRaises(IdentityError):
                    lineage.transcript_proof(str(path), self.config, self.b, self.a)
        path.write_bytes(original)
        path.chmod(0o666)
        with self.assertRaisesRegex(IdentityError, "unsafe"):
            lineage.transcript_proof(str(path), self.config, self.b, self.a)
        path.chmod(0o600)
        with patch.object(lineage, "MAX_TRANSCRIPT_BYTES", len(original)-1):
            with self.assertRaisesRegex(IdentityError, "supported limit"):
                lineage.transcript_proof(str(path), self.config, self.b, self.a)
        with patch.object(lineage, "MAX_LINE_BYTES", len(original)-1):
            with self.assertRaisesRegex(IdentityError, "line limit"):
                lineage.transcript_proof(str(path), self.config, self.b, self.a)
        actual = path.with_name("actual.jsonl")
        path.rename(actual)
        path.symlink_to(actual)
        with self.assertRaises(IdentityError):
            self.certify(self.b, observed, known)

    def test_known_cycle_reparenting_and_unbounded_observations_fail_closed(self):
        a = self.root()
        b = self.certify(self.b, self.branch(self.a, self.b), a["ancestors"])
        changed = copy.deepcopy(b["ancestors"])
        changed[-1]["parent_session_id"] = self.c
        with self.assertRaisesRegex(IdentityError, "immutable proof"):
            self.certify(self.b, known=[*b["ancestors"], changed[-1]])
        changed[-1]["parent_session_id"] = self.b
        with self.assertRaisesRegex(IdentityError, "cycles"):
            self.certify(self.b, known=[*a["ancestors"], changed[-1]])
        with patch.object(lineage, "MAX_OBSERVATIONS", 1):
            with self.assertRaisesRegex(IdentityError, "observation bound"):
                self.certify(self.a, [self.capture(source="startup")]*2)


if __name__ == "__main__":
    unittest.main()
