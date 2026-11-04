"""Registration loads creation evidence only for the native provider that needs it."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from murmur import __main__ as cli
from murmur import config, diagnostics, hooks, native
from murmur.ledger import Ledger
from murmur.coordinator import Coordinator
from murmur.backend import Backend, BackendError
from murmur.native import claude, lineage
from test_coordinator import FakeBackend


SESSION = "11111111-1111-4111-8111-111111111111"
PROCESS = {"pid": 2300, "proc_start": "fixture birth", "entrypoint": "cli",
           "version": "2.1.310", "root_start_verified": True}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        # PROCESS models the pinned macOS Claude profile on every test host.
        platform = patch.object(claude, "_platform", return_value="darwin")
        platform.start()
        self.addCleanup(platform.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = config.ledger_path(self.root)
        self.backend = FakeBackend()
        self.identity = native.NativeIdentity("codex", SESSION)
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)
        hooks.initialize(self.ledger.db)

    def register(self, name, prefix=None):
        output = io.StringIO()
        with patch.dict(os.environ, {"MURMUR_HOME": str(self.root)}), \
                patch.object(config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch.object(native, "current_identity", return_value=self.identity), \
                redirect_stdout(output):
            cli.main(["register", "--name", name] + (["--username-prefix", prefix] if prefix is not None else []))
        return json.loads(output.getvalue())

    def doctor(self, *, target_error=None):
        output = io.StringIO()
        before = tuple(self.ledger.db.iterdump())
        with patch.dict(os.environ, {"MURMUR_HOME": str(self.root)}), \
                patch.object(config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch.object(self.backend, "request", create=True, return_value={"version": "fixture"}) as health, \
                patch.object(native, "current_identity", return_value=self.identity), \
                patch.object(native, "resolve_identity", side_effect=AssertionError("Doctor must not resolve ancestry")), \
                patch.object(native, "verify_target", return_value=self.identity, side_effect=target_error), \
                patch.object(diagnostics, "route_capabilities", return_value={}), redirect_stdout(output):
            cli.main(["doctor"])
        health.assert_called_once_with("GET", "/health")
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)
        return json.loads(output.getvalue())

    def test_unenrolled_doctor_reports_unchecked_ancestry_without_enrolling(self):
        result = self.doctor()
        self.assertFalse(result["enrolled"])
        self.assertEqual(result["native"]["ancestry_status"], "not_checked")
        self.assertIsNone(result["native"]["parent_known"])
        self.assertIsNone(result["native"]["parent_session_id"])
        self.assertIn("not been checked", result["native"]["ancestry_note"])
        self.assertEqual(self.ledger.actors(), [])
        self.assertEqual(self.backend.register_calls, [])

    def test_enrolled_doctor_uses_verified_ancestry_even_when_target_is_unavailable(self):
        for parent in (None, "22222222-2222-4222-8222-222222222222"):
            with self.subTest(parent=parent):
                # Use separate actors to preserve immutable ancestry. The current
                # lightweight capture still lacks the enrolled parent proof.
                self.identity = native.NativeIdentity("codex", str(uuid.uuid4()))
                verified = replace(self.identity, parent_known=True, parent_session_id=parent)
                Coordinator(self.ledger, self.backend).register(verified, "Verified session")
                for error in (None, RuntimeError("Previous process exited")):
                    with self.subTest(target_error=bool(error)):
                        result = self.doctor(target_error=error)
                        self.assertEqual(result["native"]["ancestry_status"], "verified")
                        self.assertIs(result["native"]["parent_known"], True)
                        self.assertEqual(result["native"]["parent_session_id"], parent)
                        self.assertEqual(result["native"]["ok"], error is None)
                        if error:
                            self.assertEqual(result["native"]["error"], str(error))
        self.assertEqual(len(self.ledger.actors()), 2)  # No parent was enrolled.

    def test_manual_doctor_keeps_unknown_ancestry_separate_from_manual_capabilities(self):
        Coordinator(self.ledger, self.backend).register(self.identity, mode="manual")
        result = self.doctor()
        self.assertEqual(result["native"]["ancestry_status"], "not_checked")
        self.assertIsNone(result["native"]["parent_known"])
        self.assertEqual(result["native"]["transport"], "manual")
        self.assertFalse(result["enrollment"]["inherited_access"])
        self.assertFalse(result["enrollment"]["automatic_wakeups"])

    def test_explicit_task_prefix_survives_cli_rename_and_preserves_native_identity(self):
        with patch.object(native, "resolve_identity", return_value=replace(self.identity, parent_known=True)):
            first = self.register("Exact native title", "checkout-review")
            second = self.register("Renamed native title")
            third = self.register("Renamed native title", "api-review")
        self.assertEqual(self.backend.register_prefixes, ["checkout-review", "checkout-review", "api-review"])
        self.assertEqual({first["user_id"], second["user_id"], third["user_id"]}, {"self"})
        self.assertEqual(self.ledger.actor(self.identity.actor)["name"], "Renamed native title")
        self.assertEqual(self.ledger.actor(self.identity.actor)["username_prefix"], "api-review")

    def test_prefix_is_persisted_before_provisioning_and_reused_after_restart(self):
        identity = replace(self.identity, parent_known=True)
        coordinator = Coordinator(self.ledger, self.backend)
        with patch.object(self.backend, "register", side_effect=BackendError("Request never arrived", uncertain=True)):
            with self.assertRaises(BackendError):
                coordinator.register(identity, "Exact title", username_prefix="database-work")
        self.assertIsNone(self.ledger.actor(identity.actor)["user_id"])
        restarted = Ledger(self.path)
        try:
            Coordinator(restarted, self.backend).sync(identity.actor)
            self.assertEqual(restarted.actor(identity.actor)["user_id"], "self")
            self.assertEqual(self.backend.register_prefixes, ["database-work"])
        finally:
            restarted.close()

    def test_invalid_prefix_never_enrolls_or_provisions(self):
        coordinator = Coordinator(self.ledger, self.backend)
        for prefix in ("", "  ", "x" * 65, 42):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "Username prefix"):
                coordinator.register(replace(self.identity, parent_known=True), "Exact title", username_prefix=prefix)
        self.assertEqual(self.ledger.actors(), [])
        self.assertEqual(self.backend.register_calls, [])

    def test_backend_omits_unspecified_prefix_and_forwards_literal_explicit_prefix(self):
        backend = Backend("http://fixture.invalid", "fixture")
        with patch.object(backend, "request", return_value={}) as request:
            backend.register(self.identity.actor, "Exact title", mode="native",
                             lineage=[{"actor": self.identity.actor, "parent": None}])
            self.assertNotIn("username_prefix", request.call_args.args[2])
            backend.register(self.identity.actor, "Exact title", username_prefix="API review", mode="native",
                             lineage=[{"actor": self.identity.actor, "parent": None}])
            self.assertEqual(request.call_args.args[2]["username_prefix"], "API review")

    def capture(self, count):
        observations = [lineage.observation({
            "hook_event_name": "SessionStart", "session_id": str(uuid.UUID(int=i + 1, version=4)),
            "source": "startup"}, PROCESS) for i in range(count)]
        with self.ledger._write():
            self.ledger.db.executemany("INSERT INTO native_observations(evidence,captured_at) VALUES(?,?)",
                                      [(json.dumps(item, sort_keys=True), 100) for item in observations])
        return self.observations()

    def observations(self):
        return [tuple(row) for row in self.ledger.db.execute(
            "SELECT id,evidence,captured_at FROM native_observations ORDER BY id")]

    def test_codex_registration_and_rename_ignore_unrelated_claude_evidence_above_bound(self):
        before = self.capture(4097)
        resolved = replace(self.identity, parent_known=True)
        with patch.object(native, "resolve_identity", return_value=resolved) as resolve:
            first = self.register("Original title")
            renamed = self.register("Current title")
        self.assertEqual(resolve.call_count, 2)
        self.assertTrue(all(call.args == (self.identity,) and call.kwargs == {
            "lineage_proof": {"observations": [], "known": []}}
                            for call in resolve.call_args_list))
        self.assertEqual((first["id"], first["user_id"]), (renamed["id"], renamed["user_id"]))
        self.assertEqual(self.backend.register_calls, [
            (self.identity.actor, "Original title", None), (self.identity.actor, "Current title", None)])
        record = self.ledger.actor(self.identity.actor)
        self.assertEqual((record["name"], record["mode"], record["identity"]["parent_known"]),
                         ("Current title", "native", True))
        self.assertEqual(self.observations(), before)

    def test_desktop_registration_bypasses_unrelated_cli_evidence_and_retains_audit(self):
        before = self.capture(4097)
        self.identity = native.NativeIdentity("claude", "local_" + SESSION, target={"runtime": "desktop"})
        resolved = replace(self.identity, parent_known=True)
        with patch.object(native, "resolve_identity", return_value=resolved) as resolve:
            self.register("Exact Desktop title")
        self.assertEqual(resolve.call_args.args, (self.identity,))
        self.assertEqual(resolve.call_args.kwargs["lineage_proof"], {"observations": [], "known": []})
        self.assertEqual(len(self.backend.register_calls), 1)
        self.assertEqual(self.observations(), before)

    def test_cli_registration_scopes_unrelated_lifetime_evidence_and_keeps_failures(self):
        self.capture(4097)
        self.identity = native.NativeIdentity("claude", SESSION, target={
            **PROCESS, "runtime": "cli", "config_dir": str(self.root)})
        observed = lineage.observation({"hook_event_name": "SessionStart", "session_id": SESSION,
                                        "source": "startup"}, PROCESS)
        self.ledger.db.execute("INSERT INTO native_observations(evidence,captured_at) VALUES(?,100)",
                               (json.dumps(observed, sort_keys=True),))
        before = self.observations()
        with patch.object(claude, "verify_target", return_value=self.identity):
            first = self.register("Exact native title")
            renamed = self.register("Renamed native title")
        self.assertEqual(first["id"], renamed["id"])
        self.assertEqual(len(self.backend.register_calls), 2)
        self.assertEqual(self.observations(), before)

    def test_claude_registration_certifies_recorded_startup_and_rejects_changed_evidence(self):
        self.identity = native.NativeIdentity("claude", SESSION, target={
            **PROCESS, "runtime": "cli", "config_dir": str(self.root)})
        observed = lineage.observation({"hook_event_name": "SessionStart", "session_id": SESSION,
                                        "source": "startup"}, PROCESS)
        self.ledger.db.execute("INSERT INTO native_observations(evidence,captured_at) VALUES(?,?)",
                               (json.dumps(observed, sort_keys=True), 100))
        with patch.object(claude, "verify_target", return_value=self.identity):
            result = self.register("Verified Claude title")
        self.assertEqual(result["id"], self.identity.actor)
        record = self.ledger.actor(self.identity.actor)
        self.assertTrue(record["identity"]["parent_known"])
        proof = record["identity"]["evidence"]["parent_proof"]
        self.assertEqual((proof["session_id"], proof["proof"], proof["parent_session_id"]),
                         (SESSION, "startup", None))
        saved = self.ledger.db.execute("SELECT evidence FROM native_proofs WHERE actor=?",
                                      (self.identity.actor,)).fetchone()[0]
        self.assertEqual(json.loads(saved), proof["ancestors"][0])
        observed["event_key"] = "changed"
        self.ledger.db.execute("UPDATE native_observations SET evidence=?", (json.dumps(observed),))
        with patch.object(claude, "verify_target", return_value=self.identity):
            with self.assertRaisesRegex(native.IdentityError, "immutable observation changed"):
                self.register("Unverified rename")
        self.assertEqual(self.ledger.actor(self.identity.actor), record)
        self.assertEqual(len(self.backend.register_calls), 1)


if __name__ == "__main__":
    unittest.main()
