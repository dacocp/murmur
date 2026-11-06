"""Verified current actors, pure creation capture and fake socket admission."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import errno
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from murmur import native
from murmur.native import claude, lineage, process
from murmur.native.types import IdentityError


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.config = self.home / ".claude"
        (self.config / "sessions").mkdir(parents=True)
        self.engine, self.other = str(uuid.uuid4()), str(uuid.uuid4())
        self.host = "local_" + str(uuid.uuid4())
        self.birth, self.uid = "Sun Oct 25 08:00:00 2026", os.getuid()
        self.record = dict(pid=2300, pidDomain="darwin", procStart=self.birth,
                           sessionId=self.engine, hostSessionId=self.host,
                           entrypoint="claude-desktop", status="idle")
        self.registry = self.config / "sessions/2300.json"
        self.write_record()
        self.meta = self.home / "metadata"
        self.env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config),
                    "CLAUDE_PID": "2300", "CLAUDE_CODE_SESSION_ID": self.engine,
                    "CLAUDE_CODE_HOST_SESSION_ID": self.host,
                    "MURMUR_CLAUDE_METADATA_ROOT": str(self.meta)}
        self.processes = {2400: (2300, self.uid, "shell birth"),
                          2300: (2200, self.uid, self.birth),
                          2200: (1, self.uid, "outer birth")}
        for replacement in (
            patch.object(claude, "_platform", return_value="darwin"),
            patch.object(os, "getppid", return_value=2400),
            patch.object(process, "identity", side_effect=lambda pid, timeout=3: self.processes[pid]),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def write_record(self, **changed):
        self.record.update(changed)
        self.registry.write_text(json.dumps(self.record))

    def current(self, payload=None):
        return claude.current_identity(payload, env=self.env)

    def mirror(self, host=None, parent=None, engine=None, account="one", **extra):
        host = host or self.host
        path = self.meta / account / "account" / f"{host}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(sessionId=host, cliSessionId=engine or self.engine,
                                        forkedFromSessionId=parent, **extra)))
        return path

    @contextmanager
    def cli(self, argv=None, *, version="2.1.310", managed=False):
        content = b"fake pinned executable"
        digest = hashlib.sha256(content).hexdigest()
        executable = (self.home / ".local/share/murmur/native-cli/claude" / digest / "package/claude"
                      if managed else self.home / ".local/bin/claude")
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(content)
        original = self.record.copy()
        self.write_record(hostSessionId=None, entrypoint="cli", kind="interactive", version=version)
        constant = "MAC_CLI_317_SHA256" if version == "2.1.317" else "CLI_SHA256"
        try:
            with patch.object(process, "inspect", return_value=(str(executable), tuple(argv or (str(executable),)))), \
                    patch.object(claude, constant, digest):
                yield executable
        finally:
            self.record = original
            self.write_record()

    @contextmanager
    def idle_replacement(self, identity=None, pid=2500, **changed):
        """Owned registry/socket fixture; process inspection never uses real PIDs."""
        if identity is None:
            self.mirror()
            identity = claude.resolve_identity(self.current())
        original, processes = self.registry.read_bytes(), self.processes.copy()
        parent = self.home / "idle-recovery"
        parent.mkdir(mode=0o700, exist_ok=True)
        endpoint = parent / "native.sock"
        path = self.config / "sessions" / f"{pid}.json"
        record = {**self.record, "pid": pid, "procStart": "replacement birth",
                  "messagingSocketPath": str(endpoint), **changed}
        self.registry.unlink()
        path.write_text(json.dumps(record))
        self.processes.pop(2300)
        self.processes[pid] = (1, self.uid, record["procStart"])

        def probe(current, signal):
            self.assertEqual(signal, 0)
            if current not in self.processes:
                raise ProcessLookupError(errno.ESRCH, "fixture owner ended")

        try:
            with socket.socket(socket.AF_UNIX) as server, patch.object(os, "kill", side_effect=probe):
                server.bind(str(endpoint))
                yield identity, record, path
        finally:
            self.processes = processes
            path.unlink(missing_ok=True)
            self.registry.write_bytes(original)
            endpoint.unlink(missing_ok=True)

    def test_current_identity_binds_exact_invoker_and_host_not_engine_alias(self):
        identity = self.current()
        self.assertEqual(identity.session_id, self.host)
        self.assertEqual(identity.target["engine_id"], self.engine)
        self.assertFalse(identity.parent_known)
        self.assertEqual(json.loads(identity.epoch), [self.engine, 2300, self.birth])
        without_engine = {k: v for k, v in self.env.items() if k != "CLAUDE_CODE_SESSION_ID"}
        self.assertEqual(claude.current_identity({"session_id": self.engine}, env=without_engine).session_id, self.host)
        with self.assertRaises(IdentityError):
            claude.current_identity(env=without_engine)

    def test_copied_host_cannot_select_outer_desktop_for_nested_cli(self):
        outer = {**self.record, "pid": 2200, "procStart": "outer birth"}
        (self.config / "sessions/2200.json").write_text(json.dumps(outer))
        with self.cli():
            identity = self.current()
            self.assertEqual(identity.session_id, self.engine)
            self.assertFalse(identity.session_id.startswith("local_"))
            with self.assertRaises(IdentityError):
                claude.resolve_identity(identity)
        self.registry.unlink()
        with self.assertRaises(IdentityError):
            self.current()

    def test_registry_epoch_owner_engine_host_and_entrypoint_mismatch_reject(self):
        original = self.record.copy()
        for changed in ({"procStart": "old birth"}, {"sessionId": self.other}, {"pid": "2300"},
                        {"pidDomain": "linux"}, {"hostSessionId": "local_" + self.other},
                        {"entrypoint": "cli"}):
            with self.subTest(changed=changed):
                self.record = {**original, **changed}
                self.write_record()
                with self.assertRaises(IdentityError):
                    self.current()
        self.record = original
        self.write_record()
        self.processes[2300] = (2200, self.uid + 1, self.birth)
        with self.assertRaisesRegex(IdentityError, "ownership or birth"):
            self.current()

    def test_hook_engine_pid_conflict_subagent_and_unrelated_process_fail_closed(self):
        with self.assertRaisesRegex(IdentityError, "engine identity disagree"):
            self.current({"session_id": self.other})
        with self.assertRaises(IdentityError):
            self.current({"session_id": self.engine, "agent_id": "nested"})
        for pid in ("", "２３００", "2300 ", "0", str(2**31)):
            with self.subTest(pid=pid), self.assertRaises(IdentityError):
                claude.current_identity(env={**self.env, "CLAUDE_PID": pid})
        with self.assertRaises(IdentityError):
            claude.current_identity(env={**self.env, "CODEX_THREAD_ID": self.other})
        self.processes[2400] = (1, self.uid, "unrelated")
        with self.assertRaisesRegex(IdentityError, "invoking ancestor"):
            self.current()

    def test_desktop_direct_parent_requires_agreeing_mirrors_and_current_engine(self):
        parent = "local_" + self.other
        self.mirror(parent=parent)
        self.mirror(parent=parent, account="two")
        self.mirror(host=parent, engine=str(uuid.uuid4()))
        identity = claude.resolve_identity(self.current())
        self.assertTrue(identity.parent_known)
        self.assertEqual(identity.parent_session_id, parent)
        self.assertEqual(identity.evidence["parent_proof"]["mirror_count"], 2)
        self.mirror(parent=None, account="two")
        with self.assertRaisesRegex(IdentityError, "mirrors disagree"):
            claude.resolve_identity(self.current())
        self.mirror(parent=parent, account="two", engine=str(uuid.uuid4()))
        with self.assertRaises(IdentityError):
            claude.resolve_identity(self.current())

    def test_desktop_missing_parent_and_fork_marker_never_become_root(self):
        self.mirror(spawnedFrom={"userFork": True})
        with self.assertRaisesRegex(IdentityError, "direct fork parent"):
            claude.resolve_identity(self.current())
        self.mirror(parent="local_" + self.other)
        partial = claude.resolve_identity(self.current())
        self.assertEqual(partial.parent_session_id, "local_" + self.other)
        self.assertFalse(partial.evidence["lineage_complete"])
        self.assertIn("not persisted", partial.evidence["lineage_reason"])
        path = self.mirror()
        data = json.loads(path.read_text())
        data.pop("forkedFromSessionId")
        path.write_text(json.dumps(data))
        root = claude.resolve_identity(self.current())
        self.assertTrue(root.parent_known)
        self.assertIsNone(root.parent_session_id)
        self.assertTrue(root.evidence["lineage_complete"])

    def test_cli_creation_precedes_registry_flip_but_certification_requires_current_child(self):
        with self.cli():
            startup = dict(hook_event_name="SessionStart", session_id=self.engine, source="startup")
            observed = claude.capture_creation(startup, self.env)
            provisional = claude.identity_from_creation(observed)
            self.assertEqual(provisional.session_id, self.engine)
            self.assertIn("config_dir", observed["process"])
            known = claude.resolve_identity(self.current(), lineage_proof={"observations": [observed]})
            self.assertTrue(known.parent_known)
            self.assertIsNone(known.parent_session_id)
            prompt = str(uuid.uuid4())
            end = claude.capture_creation(dict(hook_event_name="SessionEnd", session_id=self.engine,
                                               reason="resume", prompt_id=prompt), self.env)
            child_env = {**self.env, "CLAUDE_CODE_SESSION_ID": self.other}
            project = self.config / "projects/fixture"
            project.mkdir(parents=True)
            path = project / f"{self.other}.jsonl"
            path.write_text(json.dumps(dict(type="user", sessionId=self.other,
                                            forkedFrom={"sessionId": self.engine})) + "\n")
            start = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.other,
                                                 source="fork", prompt_id=prompt,
                                                 transcript_path=str(path)), child_env)
            child = claude.identity_from_creation(start)
            proof = {"known": known.evidence["parent_proof"]["ancestors"], "observations": [end, start]}
            with self.assertRaisesRegex(IdentityError, "epoch changed"):
                claude.resolve_identity(child, lineage_proof=proof)
            self.write_record(sessionId=self.other)
            child = claude.resolve_identity(child, lineage_proof=proof)
            self.assertEqual(child.parent_session_id, self.engine)
            self.assertTrue(child.parent_known)
            self.assertTrue(child.evidence["lineage_complete"])
            self.assertEqual(child.evidence["lineage_edges"], [
                {"session_id": self.other, "parent_session_id": self.engine},
                {"session_id": self.engine, "parent_session_id": None}])

    def test_capture_desktop_returns_none_and_unknown_cli_version_or_argv_rejects(self):
        payload = dict(hook_event_name="SessionStart", session_id=self.engine, source="startup")
        self.assertIsNone(claude.capture_creation(payload, self.env))
        with self.cli(), patch.object(claude, "CLI_SHA256", "unsupported"):
            with self.assertRaisesRegex(IdentityError, "unsupported"):
                claude.capture_creation(payload, self.env)
        for args in (("ENV_SENTINEL=not argv",), ("claude", "--teleport"),
                     ("claude", "--session-id", self.engine), ("claude", "a prompt")):
            with self.subTest(args=args), self.cli(args), self.assertRaises(IdentityError):
                self.current()

    def test_mac_317_measured_creation_precedes_registry_and_keeps_verified_version(self):
        with self.cli(version="2.1.317", managed=True):
            self.registry.unlink()
            observed = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.engine,
                                                   source="startup"), self.env)
            self.assertEqual(observed["process"]["version"], "2.1.317")
            self.write_record()
            identity = claude.resolve_identity(self.current(), lineage_proof={"observations": [observed]})
            self.assertEqual(identity.target["version"], "2.1.317")
            self.assertEqual(identity.evidence["parent_proof"]["ancestors"][0]["version"], "2.1.317")
            self.assertIsNone(identity.parent_session_id)
            self.assertEqual(claude.verify_target(identity).target, identity.target)

    def test_default_cli_profile_keeps_legacy_platform_contract_without_selecting_mac_317(self):
        for platform, expected in (("darwin", (claude.CLI_VERSION, claude.CLI_SHA256)),
                                   ("linux", (claude.LINUX_CLI_VERSION, claude.LINUX_CLI_SHA256))):
            with self.subTest(platform=platform), patch.object(claude, "_platform", return_value=platform):
                self.assertEqual(claude._cli_profile(), expected)
                self.assertEqual(claude._cli_profile(None), expected)
                if platform == "darwin":
                    self.assertEqual(claude._cli_profile(claude.MAC_CLI_317_SHA256),
                                     (claude.MAC_CLI_317_VERSION, claude.MAC_CLI_317_SHA256))
                else:
                    with self.assertRaisesRegex(IdentityError, "unsupported"):
                        claude._cli_profile(claude.MAC_CLI_317_SHA256)

    def test_managed_mac_profiles_and_legacy_install_bind_registry_to_measured_hash(self):
        for version in ("2.1.310", "2.1.317"):
            for managed in (False, True):
                with self.subTest(version=version, managed=managed), self.cli(version=version, managed=managed):
                    identity = self.current()
                    self.assertEqual(identity.target["version"], version)
                    self.assertEqual(claude.verify_target(identity).target["version"], version)
                    wrong = "2.1.310" if version == "2.1.317" else "2.1.317"
                    self.write_record(version=wrong)
                    with self.assertRaisesRegex(IdentityError, "unsupported"):
                        self.current()
                    with self.assertRaisesRegex(IdentityError, "registry binding changed"):
                        claude.verify_target(identity)

    def test_managed_mac_317_rejects_wrong_hash_cache_address_or_platform(self):
        with self.cli(version="2.1.317", managed=True) as executable:
            payload = dict(hook_event_name="SessionStart", session_id=self.engine, source="startup")
            content = executable.read_bytes()
            executable.write_bytes(content + b" changed")
            with self.assertRaisesRegex(IdentityError, "unsupported"):
                claude.capture_creation(payload, self.env)
            executable.write_bytes(content)
            for path in (self.home / "arbitrary/claude",
                         self.home / ".local/share/murmur/native-cli/claude" / ("0" * 64) / "package/claude",
                         self.home / ".local/share/claude/versions/2.1.310"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                with self.subTest(path=path), patch.object(process, "inspect", return_value=(str(path), (str(path),))):
                    with self.assertRaisesRegex(IdentityError, "native installation"):
                        claude.capture_creation(payload, self.env)
            with patch.object(claude, "_platform", return_value="linux"), \
                    patch.object(process, "pid_domain", return_value="linux:" + "a" * 32 + ":pid:[4026531836]"):
                with self.assertRaisesRegex(IdentityError, "unsupported"):
                    claude._cli_facts(self.engine, 2300, self.env)

    def test_mac_317_cold_resume_and_idle_recovery_keep_exact_profile(self):
        with self.cli(version="2.1.317", managed=True):
            observed = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.engine,
                                                   source="startup"), self.env)
            identity = claude.resolve_identity(self.current(), lineage_proof={"observations": [observed]})
            with self.idle_replacement(identity=identity) as (_, record, path):
                prepared = claude.prepare_idle_target(identity)
                self.assertEqual(prepared.target["version"], "2.1.317")
                self.assertEqual(prepared.target["pid"], 2500)
                self.assertIs(prepared.evidence, identity.evidence)
                path.write_text(json.dumps({**record, "version": "2.1.310"}))
                with self.assertRaisesRegex(IdentityError, "registry binding changed"):
                    claude.prepare_idle_target(identity)
            proof = identity.evidence["parent_proof"]
        self.birth = "later PID birth"
        self.processes[2300] = (2200, self.uid, self.birth)
        self.write_record(procStart=self.birth)
        with self.cli(("claude", "--resume", self.engine), version="2.1.317", managed=True):
            resumed = claude.resolve_identity(self.current(), lineage_proof=proof)
            self.assertEqual(resumed.target["version"], "2.1.317")
            self.assertNotEqual(resumed.epoch, identity.epoch)
            self.assertEqual(resumed.evidence["parent_proof"]["ancestors"], proof["ancestors"])

    def test_known_cli_cold_resume_and_custom_config_use_recorded_home_not_config_parent(self):
        custom = self.home / "separate/config"
        custom.parent.mkdir()
        self.config.rename(custom)
        self.config = custom
        self.registry = custom / "sessions/2300.json"
        self.env["CLAUDE_CONFIG_DIR"] = str(custom)
        with self.cli():
            observation = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.engine,
                                                       source="startup"), self.env)
            identity = claude.resolve_identity(self.current(), lineage_proof={"observations": [observation]})
            proof = identity.evidence["parent_proof"]
        self.birth = "later PID birth"
        self.processes[2300] = (2200, self.uid, self.birth)
        self.write_record(procStart=self.birth)
        with self.cli(("claude", "--resume", self.engine)):
            resumed = claude.resolve_identity(self.current(), lineage_proof=proof)
            self.assertNotEqual(identity.epoch, resumed.epoch)
            self.assertIsNone(resumed.parent_session_id)
            self.assertTrue(resumed.parent_known)

    def test_native_branch_after_cold_resume_keeps_original_resume_process_profile(self):
        with self.cli():
            observation = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.engine,
                                                       source="startup"), self.env)
            root = claude.resolve_identity(self.current(), lineage_proof={"observations": [observation]})
        with self.cli(("claude", "--resume", self.engine)):
            prompt = str(uuid.uuid4())
            end = claude.capture_creation(dict(hook_event_name="SessionEnd", session_id=self.engine,
                                               reason="resume", prompt_id=prompt), self.env)
            child_env = {**self.env, "CLAUDE_CODE_SESSION_ID": self.other}
            project = self.config / "projects/fixture"
            project.mkdir(parents=True)
            path = project / f"{self.other}.jsonl"
            path.write_text(json.dumps(dict(type="user", sessionId=self.other,
                                            forkedFrom={"sessionId": self.engine})) + "\n")
            start = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.other,
                                                 source="fork", prompt_id=prompt,
                                                 transcript_path=str(path)), child_env)
            self.assertFalse(start["process"]["root_start_verified"])
            self.write_record(sessionId=self.other)
            child = claude.resolve_identity(claude.identity_from_creation(start), lineage_proof={
                "known": root.evidence["parent_proof"]["ancestors"], "observations": [end, start]})
            self.assertEqual(child.parent_session_id, self.engine)

    def test_verify_target_rejects_stale_engine_pid_birth_and_keeps_exact_target(self):
        identity = self.current()
        self.assertEqual(claude.verify_target(identity).epoch, identity.epoch)
        self.write_record(sessionId=self.other)
        with self.assertRaisesRegex(IdentityError, "epoch changed"):
            claude.verify_target(identity)
        self.write_record(sessionId=self.engine, procStart="reused PID")
        with self.assertRaisesRegex(IdentityError, "epoch changed"):
            claude.verify_target(identity)

    def test_desktop_parent_resolution_shares_one_deadline(self):
        parent = "local_" + self.other
        self.mirror(parent=parent)
        self.mirror(host=parent)
        identity = self.current()
        with patch.object(claude, "verify_target", return_value=identity), \
                patch.object(claude.time, "monotonic", side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(IdentityError, "deadline|bounded mirror"):
                claude.resolve_identity(identity, timeout=3)

    def test_desktop_ancestry_keeps_unenrolled_intermediate_edges_and_rejects_cycles(self):
        parent, root = "local_" + self.other, "local_" + str(uuid.uuid4())
        self.mirror(parent=parent)
        self.mirror(host=parent, parent=root, engine=str(uuid.uuid4()))
        self.mirror(host=root, engine=str(uuid.uuid4()))
        result = claude.resolve_identity(self.current())
        self.assertTrue(result.evidence["lineage_complete"])
        self.assertEqual(result.evidence["lineage_edges"], [
            {"session_id": self.host, "parent_session_id": parent},
            {"session_id": parent, "parent_session_id": root},
            {"session_id": root, "parent_session_id": None}])
        self.mirror(host=parent, parent=self.host, engine=str(uuid.uuid4()))
        with self.assertRaisesRegex(IdentityError, "cycle"):
            claude.resolve_identity(self.current())

    def test_other_writable_registry_and_mirrors_are_not_verified_metadata(self):
        self.registry.chmod(0o666)
        with self.assertRaisesRegex(IdentityError, "unsafe"):
            self.current()
        self.registry.chmod(0o600)
        path = self.mirror()
        path.chmod(0o664)
        with self.assertRaisesRegex(IdentityError, "unsafe"):
            claude.resolve_identity(self.current())

    def test_cli_profile_hashing_uses_the_callers_deadline_and_rejects_late_facts(self):
        now = [0]
        with self.cli():
            def late_hash(path, deadline):
                self.assertEqual(deadline, .5)
                now[0] = 1
                return claude.CLI_SHA256
            with patch.object(claude.files.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(claude.files, "executable_sha256", side_effect=late_hash):
                with self.assertRaisesRegex(IdentityError, "deadline"):
                    claude._cli_facts(self.engine, 2300, self.env, invoking=False, timeout=.5)

    def test_metadata_path_replacement_during_read_does_not_verify_the_open_old_inode(self):
        replacement = self.registry.with_suffix(".new")
        replacement.write_bytes(self.registry.read_bytes())
        original, calls = os.fstat, []
        def replace_after_read(descriptor):
            calls.append(descriptor)
            result = original(descriptor)
            if len(calls) == 2:
                os.replace(replacement, self.registry)
            return result
        with patch.object(claude.os, "fstat", side_effect=replace_after_read):
            with self.assertRaisesRegex(IdentityError, "unsafe"):
                claude._read_json(self.registry)

    def test_delivery_rejects_ambiguous_live_recipient_but_skips_proven_exited_record(self):
        identity = self.current()
        duplicate = {**self.record, "pid": 2200, "procStart": "outer birth"}
        (self.config / "sessions/2200.json").write_text(json.dumps(duplicate))
        with patch.object(os, "kill", return_value=None):
            result = claude.deliver(identity, "doorbell", "token", timeout=0.1)
        self.assertEqual(result.status, "rejected")
        self.assertIn("ambiguous", result.detail)
        with patch.object(os, "kill", side_effect=PermissionError("no inspection")):
            result = claude.deliver(identity, "doorbell", "token", timeout=0.1)
        self.assertEqual(result.status, "rejected")
        self.assertIn("cannot be inspected", result.detail)
        with patch.object(os, "kill", side_effect=ProcessLookupError("exited")):
            # No socket is installed here: reaching the socket error establishes
            # that a proven stale record did not fail the uniqueness check.
            result = claude.deliver(identity, "doorbell", "token", timeout=0.1)
        self.assertEqual(result.status, "rejected")
        self.assertIn("socket is unavailable", result.detail)

    def test_cli_process_epoch_change_during_argument_inspection_rejects(self):
        with self.cli(), patch.object(process, "identity", side_effect=[
                (1, self.uid, self.birth), (1, self.uid, "reused PID")]):
            with self.assertRaisesRegex(IdentityError, "epoch changed"):
                claude._cli_facts(self.engine, 2300, self.env, invoking=False)

    def test_owner_exit_requires_positive_pid_absence_and_retains_exact_recorded_epoch(self):
        identity = self.current()
        with patch.object(os, "kill", side_effect=ProcessLookupError(errno.ESRCH, "exited")) as kill, \
                patch.object(process, "identity", side_effect=AssertionError("process inspection")), \
                patch.object(claude, "_read_json", side_effect=AssertionError("registry read")):
            result = claude.owner_process_state(identity)
        self.assertEqual(result["status"], "ended")
        self.assertEqual(result["reason"], "pid_missing")
        self.assertEqual(result["recorded"], {key: identity.target[key] for key in
                                             ("pid", "proc_start", "engine_id", "native_epoch")})
        self.assertNotIn("observed", result)
        kill.assert_called_once_with(2300, 0)

    def test_same_owner_birth_is_alive_despite_new_engine_or_missing_registry(self):
        identity = self.current()
        self.write_record(sessionId=self.other)
        for registry_exists in (True, False):
            if not registry_exists:
                self.registry.unlink()
            with self.subTest(registry_exists=registry_exists), patch.object(os, "kill", return_value=None), \
                    patch.object(claude, "_read_json", side_effect=AssertionError("registry read")):
                result = claude.owner_process_state(identity)
            self.assertEqual(result["status"], "alive")
            self.assertEqual(result["reason"], "same_process")
            self.assertEqual(result["observed"], {"uid": self.uid, "proc_start": self.birth})
            self.assertEqual(result["recorded"]["engine_id"], self.engine)

    def test_positive_different_birth_proves_pid_reuse_even_for_another_current_owner(self):
        identity = self.current()
        for owner in (self.uid, self.uid + 1):
            with self.subTest(owner=owner), patch.object(os, "kill", return_value=None), \
                    patch.object(process, "identity", return_value=(1, owner, "positively new birth")):
                result = claude.owner_process_state(identity)
            self.assertEqual(result["status"], "ended")
            self.assertEqual(result["reason"], "pid_reused")
            self.assertEqual(result["observed"], {"uid": owner, "proc_start": "positively new birth"})

    def test_owner_probe_denial_or_other_failure_is_unknown_without_inspection(self):
        identity = self.current()
        for error in (PermissionError(errno.EPERM, "denied"), OSError(errno.EIO, "unavailable")):
            with self.subTest(error=error), patch.object(os, "kill", side_effect=error), \
                    patch.object(process, "identity", side_effect=AssertionError("inspection started")):
                result = claude.owner_process_state(identity)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("observed", result)

    def test_inspection_timeout_malformed_and_same_birth_wrong_owner_are_unknown(self):
        identity = self.current()
        for error in (TimeoutError("timeout"), IdentityError("ps failed"), ValueError("malformed")):
            with self.subTest(error=error), patch.object(os, "kill", return_value=None) as kill, \
                    patch.object(process, "identity", side_effect=error):
                result = claude.owner_process_state(identity, timeout=0.1)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["reason"], "inspection_unavailable")
            self.assertEqual(kill.call_count, 2)
        for value in (None, (1, self.uid, ""), (1, True, self.birth), (1, self.uid, "x" * 257)):
            with self.subTest(value=value), patch.object(os, "kill", return_value=None), \
                    patch.object(process, "identity", return_value=value):
                self.assertEqual(claude.owner_process_state(identity)["status"], "unknown")
        with patch.object(os, "kill", return_value=None), \
                patch.object(process, "identity", return_value=(1, self.uid + 1, self.birth)):
            result = claude.owner_process_state(identity)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "owner_changed")

    def test_exit_between_probe_and_inspection_needs_final_esrch(self):
        identity = self.current()
        for final, expected in ((ProcessLookupError(errno.ESRCH, "exited"), "ended"),
                                (PermissionError(errno.EPERM, "denied"), "unknown"),
                                (None, "unknown")):
            with self.subTest(final=final), patch.object(os, "kill", side_effect=[None, final]) as kill, \
                    patch.object(process, "identity", side_effect=IdentityError("vanished during inspection")):
                result = claude.owner_process_state(identity)
            self.assertEqual(result["status"], expected)
            self.assertEqual(kill.call_count, 2)

    def test_malformed_recorded_target_or_timeout_never_probes_process_or_registry(self):
        identity = self.current()
        for changes in ({"pid": True}, {"pid": -1}, {"proc_start": None},
                        {"proc_start": "x" * 257}, {"proc_start": "line\nbreak"},
                        {"engine_id": "not UUID"}, {"native_epoch": "mismatched"}, {"runtime": "cli"}):
            malformed = replace(identity, target={**identity.target, **changes})
            with self.subTest(changes=changes), patch.object(os, "kill", side_effect=AssertionError("pid probe")), \
                    patch.object(process, "identity", side_effect=AssertionError("process inspection")), \
                    patch.object(claude, "_read_json", side_effect=AssertionError("registry read")):
                result = claude.owner_process_state(malformed)
            self.assertEqual(result, {"status": "unknown", "reason": "invalid_recorded_target", "recorded": {}})
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), patch.object(os, "kill", side_effect=AssertionError("pid probe")):
                self.assertEqual(claude.owner_process_state(identity, timeout=timeout)["status"], "unknown")

    def test_idle_recovery_finds_same_engine_new_pid_without_weakening_strict_verification(self):
        with self.idle_replacement(entrypoint="claude-desktop-3p") as (identity, record, path):
            before = path.read_bytes()
            with self.assertRaises(IdentityError):
                claude.verify_target(identity)
            prepared = native.prepare_idle_target(identity)
            recovered = native.recover_idle_target(identity)
            self.assertEqual(prepared, recovered)
            # A safely retained registry for the positively ended old owner is
            # also stale evidence, never a second live recipient.
            self.write_record()
            self.assertEqual(claude.recover_idle_target(identity), recovered)
            self.registry.unlink()
            self.assertEqual(prepared.session_id, self.host)
            self.assertEqual(prepared.target["engine_id"], self.engine)
            self.assertEqual(prepared.target["pid"], 2500)
            self.assertEqual(json.loads(prepared.epoch), [self.engine, 2500, record["procStart"]])
            self.assertNotEqual(prepared.epoch, identity.epoch)
            self.assertIs(prepared.evidence, identity.evidence)
            self.assertEqual((prepared.parent_known, prepared.parent_session_id),
                             (identity.parent_known, identity.parent_session_id))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(identity.target["pid"], 2300)
            with self.assertRaisesRegex(IdentityError, "previously certified"):
                claude.recover_idle_target(replace(identity, parent_known=False))
            with patch.object(socket, "socket", side_effect=AssertionError("socket submission")):
                self.assertEqual(claude.deliver(identity, "doorbell", "frozen-token").status, "rejected")
            with self.assertRaises(IdentityError):
                claude.verify_target(identity)

    def test_idle_preparation_refreshes_same_owner_socket_and_holds_busy_before_claim(self):
        self.mirror()
        identity = claude.resolve_identity(self.current())
        parent = self.home / "same-owner"
        parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as server:
            endpoint = parent / "native.sock"
            server.bind(str(endpoint))
            self.write_record(messagingSocketPath=str(endpoint))
            with patch.object(claude, "_recover_idle_record", side_effect=AssertionError("rediscovery")):
                prepared = claude.prepare_idle_target(identity)
                self.assertEqual(prepared.epoch, identity.epoch)
                self.assertEqual(prepared.target["socket_path"], str(endpoint))
                for status in ("busy", "running", None):
                    self.write_record(status=status)
                    with self.subTest(status=status), self.assertRaisesRegex(IdentityError, "idle status"):
                        claude.prepare_idle_target(identity)
            self.write_record(status="idle")
            with patch.object(claude, "_unique_recipient", side_effect=lambda *a, **k: self.write_record(status="busy")):
                with self.assertRaisesRegex(IdentityError, "changed before idle preparation"):
                    claude.prepare_idle_target(identity)

    def test_missing_old_registry_and_busy_replacement_hold_until_explicit_idle(self):
        with self.idle_replacement(status="busy") as (identity, record, path):
            with self.assertRaisesRegex(IdentityError, "idle status"):
                claude.prepare_idle_target(identity)
            record["status"] = "idle"
            path.write_text(json.dumps(record))
            self.assertEqual(claude.prepare_idle_target(identity).target["pid"], 2500)

    def test_missing_registry_never_proves_old_exit_or_allows_unknown_owner_discovery(self):
        with self.idle_replacement() as (identity, _, _):
            self.processes[2300] = (2200, self.uid, self.birth)
            with patch.object(claude, "_recover_idle_record", side_effect=AssertionError("rediscovery")):
                for call in (claude.prepare_idle_target, claude.recover_idle_target):
                    with self.subTest(call=call.__name__), self.assertRaisesRegex(IdentityError, "same_process"):
                        call(identity)
                with patch.object(os, "kill", side_effect=PermissionError(errno.EPERM, "denied")):
                    for call in (claude.prepare_idle_target, claude.recover_idle_target):
                        with self.subTest(call=call.__name__), self.assertRaisesRegex(IdentityError, "pid_probe_denied"):
                            call(identity)

    def test_idle_recovery_accepts_proven_old_pid_reuse_but_never_guesses_candidate_birth(self):
        with self.idle_replacement(pid=2300) as (identity, _, _):
            prepared = claude.prepare_idle_target(identity)
            self.assertEqual(prepared.target["pid"], 2300)
            self.assertEqual(prepared.target["proc_start"], "replacement birth")
            self.assertNotEqual(prepared.epoch, identity.epoch)
        with self.idle_replacement() as (identity, _, path):
            before = path.read_bytes()
            self.processes[2500] = (1, self.uid, "unrelated replacement birth")
            with self.assertRaisesRegex(IdentityError, "not loaded"):
                claude.prepare_idle_target(identity)
            self.assertEqual(path.read_bytes(), before)

    def test_idle_recovery_rejects_foreign_ambiguous_and_different_engine_recipients(self):
        with self.idle_replacement() as (identity, record, path):
            self.processes[2500] = (1, self.uid + 1, record["procStart"])
            with self.assertRaisesRegex(IdentityError, "owner_changed"):
                claude.prepare_idle_target(identity)
            self.processes[2500] = (1, self.uid, record["procStart"])
            duplicate = self.config / "sessions/2600.json"
            duplicate.write_text(json.dumps({**record, "pid": 2600}))
            self.processes[2600] = (1, self.uid, record["procStart"])
            with self.assertRaisesRegex(IdentityError, "ambiguous"):
                claude.prepare_idle_target(identity)
            duplicate.unlink()
            path.write_text(json.dumps({**record, "sessionId": self.other}))
            with self.assertRaisesRegex(IdentityError, "engine changed"):
                claude.prepare_idle_target(identity)
            path.write_text(json.dumps({**record, "hostSessionId": "local_" + self.other}))
            with self.assertRaisesRegex(IdentityError, "not loaded"):
                claude.prepare_idle_target(identity)

    def test_idle_recovery_preserves_parent_and_requires_exact_agreeing_desktop_mirrors(self):
        with self.idle_replacement() as (identity, _, _):
            for changed in ({"engine": self.other}, {"parent": "local_" + self.other}):
                self.mirror(**changed)
                with self.subTest(changed=changed), self.assertRaisesRegex(IdentityError, "engine or direct parent"):
                    claude.prepare_idle_target(identity)
            self.mirror()
            self.mirror(account="two", engine=self.other)
            with self.assertRaisesRegex(IdentityError, "mirrors disagree"):
                claude.prepare_idle_target(identity)
            self.mirror(account="two").unlink()
            self.mirror().unlink()
            with self.assertRaisesRegex(IdentityError, "not persisted"):
                claude.prepare_idle_target(identity)

    def test_idle_recovery_holds_unsafe_partial_unloaded_and_unsupported_records(self):
        with self.idle_replacement() as (identity, record, path):
            path.chmod(0o666)
            with self.assertRaisesRegex(IdentityError, "unsafe"):
                claude.prepare_idle_target(identity)
            path.chmod(0o600)
            for data in ("{", "{}", json.dumps({**record, "pidDomain": "another PID domain"}),
                         json.dumps({**record, "pid": 2600}),
                         json.dumps({**record, "entrypoint": "remote"})):
                path.write_text(data)
                with self.subTest(data=data), self.assertRaises(IdentityError):
                    claude.prepare_idle_target(identity)
            path.write_text(json.dumps(record))
            partial = self.config / "sessions/2600.json"
            partial.write_text("{}")
            with self.assertRaises(IdentityError):
                claude.prepare_idle_target(identity)
            partial.unlink()
            path.unlink()
            with self.assertRaisesRegex(IdentityError, "not loaded"):
                claude.prepare_idle_target(identity)

    def test_idle_preparation_excludes_only_complete_different_engine_unbound_vscode_rows(self):
        with self.idle_replacement() as (identity, record, path):
            unbound = {**record, "pid": 2600, "sessionId": self.other, "kind": "interactive",
                       "entrypoint": "claude-vscode", "version": "2.1.310"}
            unbound.pop("hostSessionId")
            other_path = self.config / "sessions/2600.json"
            other_path.write_text(json.dumps(unbound))
            self.processes[2600] = (1, self.uid, unbound["procStart"])
            prepared = claude.prepare_idle_target(identity)
            self.assertEqual(prepared.target["pid"], 2500)
            self.assertEqual(claude.prepare_idle_target(prepared), prepared)
            for changed in ({"sessionId": self.engine}, {"entrypoint": "claude-desktop"},
                            {"sessionId": "not-canonical"}, {"procStart": ""}):
                other_path.write_text(json.dumps({**unbound, **changed}))
                with self.subTest(changed=changed), self.assertRaises(IdentityError):
                    claude.prepare_idle_target(identity)
            other_path.write_text(json.dumps({"sessionId": self.other, "entrypoint": "claude-vscode"}))
            with self.assertRaisesRegex(IdentityError, "incomplete"):
                claude.prepare_idle_target(identity)
            other_path.write_text(json.dumps(unbound))
            other_path.chmod(0o666)
            with self.assertRaisesRegex(IdentityError, "unsafe"):
                claude.prepare_idle_target(identity)
            other_path.chmod(0o600)
            path.unlink()
            with self.assertRaisesRegex(IdentityError, "not loaded"):
                claude.prepare_idle_target(identity)

    def test_idle_preparation_rejects_unsafe_registry_socket_and_unbounded_scans(self):
        with self.idle_replacement() as (identity, record, path):
            sessions = self.config / "sessions"
            sessions.chmod(0o777)
            with self.assertRaisesRegex(IdentityError, "directory is unsafe"):
                claude.prepare_idle_target(identity)
            sessions.chmod(0o700)
            socket_parent = Path(record["messagingSocketPath"]).parent
            socket_parent.chmod(0o777)
            with self.assertRaisesRegex(IdentityError, "socket is unsafe"):
                claude.prepare_idle_target(identity)
            socket_parent.chmod(0o700)
            unrelated = sessions / "2600.json"
            unrelated.write_text(json.dumps({**record, "pid": 2600, "hostSessionId": "local_" + self.other}))
            with patch.object(claude, "MAX_RECIPIENT_RECORDS", 1):
                with self.assertRaisesRegex(IdentityError, "bounded"):
                    claude.prepare_idle_target(identity)

    def test_idle_recovery_scan_and_mirror_reads_share_the_original_deadline(self):
        with self.idle_replacement() as (identity, _, _):
            now, original = [0], claude._desktop_record
            def late_mirror(root, host, deadline):
                self.assertEqual(deadline, .5)
                result = original(root, host, deadline)
                now[0] = 1
                return result
            with patch.object(claude.files.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(claude, "_desktop_record", side_effect=late_mirror):
                with self.assertRaisesRegex(IdentityError, "deadline|bounded"):
                    claude.prepare_idle_target(identity, timeout=.5)

    def test_idle_recovery_preserves_cli_profile_checks_and_shared_hash_deadline(self):
        with self.cli():
            observed = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.engine,
                                                   source="startup"), self.env)
            identity = claude.resolve_identity(self.current(), lineage_proof={"observations": [observed]})
            with self.idle_replacement(identity=identity) as (_, record, path):
                prepared = claude.prepare_idle_target(identity)
                self.assertEqual(prepared.target["version"], "2.1.310")
                self.assertIs(prepared.evidence, identity.evidence)
                path.write_text(json.dumps({**record, "version": "2.1.311"}))
                with self.assertRaisesRegex(IdentityError, "registry binding changed"):
                    claude.prepare_idle_target(identity)
                path.write_text(json.dumps(record))
                with patch.object(claude, "CLI_SHA256", "unqualified"):
                    with self.assertRaisesRegex(IdentityError, "unsupported"):
                        claude.prepare_idle_target(identity)
                now = [0]
                def late_hash(path, deadline):
                    self.assertEqual(deadline, .5)
                    now[0] = 1
                    return claude.CLI_SHA256
                with patch.object(claude.files.time, "monotonic", side_effect=lambda: now[0]), \
                        patch.object(claude.files, "executable_sha256", side_effect=late_hash):
                    with self.assertRaisesRegex(IdentityError, "deadline"):
                        claude.prepare_idle_target(identity, timeout=.5)

    def test_idle_preparation_keeps_codex_strict_queue_verification_and_validates_timeouts(self):
        identity = native.NativeIdentity("codex", self.engine)
        with patch.object(native, "verify_target", return_value=identity) as verify:
            self.assertIs(native.prepare_idle_target(identity, timeout=.7), identity)
            verify.assert_called_once_with(identity, timeout=.7)
        with self.assertRaises(IdentityError):
            native.recover_idle_target(identity)
        for timeout in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), patch.object(os, "kill", side_effect=AssertionError("owner probe")):
                with self.assertRaises(IdentityError):
                    claude.prepare_idle_target(self.current(), timeout=timeout)

    def test_idle_recovery_keeps_linux_cli_profile_and_exact_pid_namespace(self):
        domain = "linux:" + "a" * 32 + ":pid:[4026531836]"
        with self.cli() as executable, patch.object(claude, "_platform", return_value="linux"), \
                patch.object(process, "pid_domain", return_value=domain), \
                patch.object(claude, "LINUX_CLI_SHA256", hashlib.sha256(executable.read_bytes()).hexdigest()):
            self.write_record(pidDomain=domain, version="2.1.311", procStart="2250001234")
            self.processes[2300] = (2200, self.uid, "2250001234")
            observed = claude.capture_creation(dict(hook_event_name="SessionStart", session_id=self.engine,
                                                   source="startup"), self.env)
            identity = claude.resolve_identity(self.current(), lineage_proof={"observations": [observed]})
            with self.idle_replacement(identity=identity, procStart="2250002222"):
                prepared = claude.prepare_idle_target(identity)
                self.assertEqual(prepared.target["version"], "2.1.311")
                self.assertEqual(prepared.target["pid_domain"], domain)
                self.assertEqual(json.loads(prepared.epoch), [self.engine, 2500, "2250002222", domain])
                with patch.object(process, "pid_domain", return_value=domain.replace("a", "b")), \
                        patch.object(os, "kill", side_effect=AssertionError("foreign namespace probe")):
                    with self.assertRaisesRegex(IdentityError, "invalid_recorded_target"):
                        claude.prepare_idle_target(identity)

    @contextmanager
    def system_tmp_socket(self, *, alias_uid=0, alias_mode=stat.S_IFLNK | 0o755,
                          alias_target="/private/tmp", root_uid=0,
                          root_mode=stat.S_IFDIR | 0o1777, canonical_root="/private/tmp",
                          wrong_socket_owner=False, wrong_parent_owner=False):
        """Map exact synthetic /tmp→/private/tmp paths onto owned temp fixtures."""
        physical = self.home / "system-alias-fixture"
        parent = physical / "cc-socks"
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        endpoint = parent / "48356.sock"
        logical = Path("/private/tmp/murmur-socket-fixture")
        original_lstat, original_resolve = Path.lstat, Path.resolve

        def mapped(path):
            try:
                return physical / path.relative_to(logical)
            except ValueError:
                return path

        def lstat(path):
            if path == Path("/tmp"):
                return SimpleNamespace(st_mode=alias_mode, st_uid=alias_uid)
            if path == Path("/private/tmp"):
                return SimpleNamespace(st_mode=root_mode, st_uid=root_uid)
            actual = mapped(path)
            info = original_lstat(actual)
            if ((wrong_socket_owner and actual == endpoint)
                    or (wrong_parent_owner and actual == parent)):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=self.uid + 1)
            return info

        def resolve(path, strict=False):
            if path == Path("/private/tmp"):
                return Path(canonical_root)
            actual = mapped(path)
            resolved = original_resolve(actual, strict=strict)
            if actual != path:
                try:
                    return logical / resolved.relative_to(physical)
                except ValueError:
                    return resolved  # Escaping aliases must still fail safety checks.
            return resolved

        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(endpoint))
            os.chmod(endpoint, 0o600)
            with patch.object(claude.sys, "platform", "darwin"), \
                    patch.object(Path, "lstat", new=lstat), patch.object(Path, "resolve", new=resolve), \
                    patch.object(os, "readlink", return_value=alias_target) as readlink:
                try:
                    yield (Path("/tmp/murmur-socket-fixture/cc-socks/48356.sock"),
                           logical / "cc-socks/48356.sock", endpoint, parent, readlink)
                finally:
                    endpoint.unlink(missing_ok=True)

    def test_only_verified_root_controlled_darwin_tmp_alias_is_expanded(self):
        for target in ("private/tmp", "/private/tmp"):
            with self.subTest(target=target), self.system_tmp_socket(alias_target=target) as fixture:
                alias, canonical, _, _, readlink = fixture
                self.assertEqual(claude._socket({"messagingSocketPath": str(alias)}), canonical)
                readlink.assert_called_once_with(Path("/tmp"))
        for changed in ({"alias_uid": 12345}, {"alias_mode": stat.S_IFDIR | 0o755},
                        {"alias_target": "/untrusted/tmp"},
                        {"root_uid": 12345}, {"root_mode": stat.S_IFDIR | 0o777},
                        {"root_mode": stat.S_IFLNK | 0o1777}, {"canonical_root": "/elsewhere/tmp"}):
            with self.subTest(changed=changed), self.system_tmp_socket(**changed) as fixture:
                with self.assertRaisesRegex(IdentityError, "trusted macOS"):
                    claude._socket({"messagingSocketPath": str(fixture[0])})
        with patch.object(claude.sys, "platform", "linux"), \
                patch.object(os, "readlink", side_effect=AssertionError("system alias inspected")):
            self.assertEqual(claude._socket_address(Path("/tmp/cc-socks/1.sock")), Path("/tmp/cc-socks/1.sock"))

    def test_tmp_alias_retains_socket_parent_owner_mode_and_symlink_rejections(self):
        for changed in ({"wrong_socket_owner": True}, {"wrong_parent_owner": True}):
            with self.subTest(changed=changed), self.system_tmp_socket(**changed) as fixture:
                with self.assertRaisesRegex(IdentityError, "unsafe or unavailable"):
                    claude._socket({"messagingSocketPath": str(fixture[0])})
        with self.system_tmp_socket() as fixture:
            alias, _, endpoint, _, _ = fixture
            endpoint.unlink()
            endpoint.write_text("a regular file is not a socket")
            with self.assertRaises(IdentityError):
                claude._socket({"messagingSocketPath": str(alias)})
        with self.system_tmp_socket() as fixture:
            alias, _, endpoint, parent, _ = fixture
            os.chmod(parent, 0o770)
            with self.assertRaises(IdentityError):
                claude._socket({"messagingSocketPath": str(alias)})
            os.chmod(parent, 0o700)
            original = endpoint.with_name("actual.sock")
            endpoint.rename(original)
            endpoint.symlink_to(original)
            with self.assertRaises(IdentityError):
                claude._socket({"messagingSocketPath": str(alias)})
            endpoint.unlink()
            original.rename(endpoint)
            renamed = parent.with_name("elsewhere")
            parent.rename(renamed)
            parent.symlink_to(renamed)
            try:
                with self.assertRaises(IdentityError):
                    claude._socket({"messagingSocketPath": str(alias)})
            finally:
                parent.unlink()
                renamed.rename(parent)

    def test_arbitrary_parent_alias_does_not_gain_system_tmp_trust(self):
        actual = self.home / "owned-sockets"
        actual.mkdir(mode=0o700)
        alias = self.home / "user-controlled-alias"
        alias.symlink_to(actual)
        with socket.socket(socket.AF_UNIX) as server:
            endpoint = actual / "native.sock"
            server.bind(str(endpoint))
            with self.assertRaisesRegex(IdentityError, "unsafe or unavailable"):
                claude._socket({"messagingSocketPath": str(alias / "native.sock")})

    @contextmanager
    def receiver(self, receipt_status="delivered", mismatch=False):
        directory = self.home / "inbox"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "native.sock"
        frame, errors = [], []
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen(1)
            server.settimeout(1)
            def receive():
                try:
                    connection, _ = server.accept()
                    with connection:
                        data = b""
                        while b"\n" not in data:
                            block = connection.recv(4096)
                            if not block:
                                return
                            data += block
                        message = json.loads(data.split(b"\n")[0])
                        frame.append(message)
                        if receipt_status is None:
                            return
                        receipt = dict(type="control", action="peer_message_status",
                                       orig_msg_id="wrong" if mismatch else message["msg_id"],
                                       status=receipt_status)
                        with socket.socket(socket.AF_UNIX) as response:
                            response.connect(message["from"][4:])
                            response.sendall(json.dumps(receipt).encode() + b"\n")
                except (OSError, ValueError) as exc:
                    errors.append(exc)
            thread = threading.Thread(target=receive)
            thread.start()
            self.write_record(messagingSocketPath=str(path))
            try:
                yield frame
            finally:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                path.unlink(missing_ok=True)
                self.assertEqual(errors, [])

    def test_fake_unix_receipt_requires_matching_orig_id_and_distinguishes_admission(self):
        for status, expected in (("delivered", "accepted"), ("denied", "rejected"),
                                 ("held", "uncertain"), (None, "uncertain")):
            with self.subTest(status=status), self.receiver(status) as frames:
                result = claude.deliver(self.current(), "doorbell", "token-123", timeout=0.1)
                self.assertEqual(result.status, expected)
                self.assertEqual(frames[0]["msg_id"], str(uuid.uuid5(uuid.NAMESPACE_URL, "murmur:token-123")))
                self.assertNotIn("session_id", frames[0])
                self.assertEqual(result.target["engine_id"], self.engine)
        with self.receiver(mismatch=True):
            self.assertEqual(claude.deliver(self.current(), "doorbell", "same-token", timeout=0.1).status, "uncertain")

    def test_cli_frame_pins_exact_engine_and_prewrite_failures_are_definite(self):
        with self.cli(), self.receiver() as frames:
            result = claude.deliver(self.current(), "doorbell", "cli-token", timeout=0.2)
            self.assertEqual(result.status, "accepted")
            self.assertEqual(frames[0]["session_id"], self.engine)
        identity = self.current()
        self.write_record(sessionId=self.other)
        result = claude.deliver(identity, "doorbell", "token", timeout=0.05)
        self.assertEqual(result.status, "rejected")
        self.assertIsNone(result.target)

    def test_prewrite_compares_validated_socket_addresses_and_rejects_changed_endpoint(self):
        with self.receiver() as frames:
            actual = Path(self.record["messagingSocketPath"])
            self.write_record(messagingSocketPath="/tmp/cc-socks/synthetic-alias.sock")
            with patch.object(claude, "_socket", return_value=actual) as address:
                result = claude.deliver(self.current(), "doorbell", "same-endpoint", timeout=0.2)
            self.assertEqual(result.status, "accepted")
            self.assertEqual(address.call_count, 2)
            self.assertEqual(len(frames), 1)
        with self.receiver() as frames:
            actual = Path(self.record["messagingSocketPath"])
            with patch.object(claude, "_socket", side_effect=[actual, actual.with_name("changed.sock")]):
                result = claude.deliver(self.current(), "doorbell", "changed-endpoint", timeout=0.2)
            self.assertEqual(result.status, "rejected")
            self.assertIn("changed before socket submission", result.detail)
            self.assertEqual(frames, [])


if __name__ == "__main__":
    unittest.main()
