"""Synthetic executable boundary tests: no installed Codex process is invoked."""
import json
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from murmur.native import codex
from murmur.native.types import DeliveryResult, IdentityError, NativeIdentity


_FAKE = r'''
import json, os, pathlib, signal, sys, time
root = pathlib.Path(__file__).parent
settings = json.loads((root / "fixture.json").read_text())
with (root / "invocations.jsonl").open("a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                         "home": os.environ.get("CODEX_HOME"),
                         "sqlite": os.environ.get("CODEX_SQLITE_HOME"),
                         "pid": os.getpid()}) + "\n")
mode = settings.get("mode", "ok")
if mode == "timeout":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
if "queue" in sys.argv:
    (root / "submitted").write_text("persisted before response")
    sys.exit(settings.get("exit", 0))
with (root / "requests.jsonl").open("a") as requests:
    def receive():
        value = json.loads(sys.stdin.readline())
        requests.write(json.dumps(value) + "\n")
        requests.flush()
        return value
    def emit(value):
        print(json.dumps(value), flush=True)
    receive()
    if mode == "malformed":
        print("{broken", flush=True)
        time.sleep(30)
    if mode == "closed":
        sys.exit(0)
    if mode == "line":
        print("x" * settings["size"], end="", flush=True)
        time.sleep(30)
    if mode == "total":
        for unused in range(settings["size"]):
            emit({"method": "notice", "params": {"padding": "x" * 80}})
        time.sleep(30)
    if mode == "early":
        emit({"id": 2, "result": {"thread": settings["thread"]}})
        time.sleep(30)
    emit({"id": 1, "result": {}})
    receive()
    request = receive()
    if mode == "error":
        emit({"id": 2, "error": {"message": "synthetic failure"}})
    elif mode == "array":
        emit([])
    elif request["method"] == "hooks/list":
        emit({"id": 2, "result": settings["hooks"]})
    else:
        thread = settings.get("threads", {}).get(request["params"]["threadId"], settings["thread"])
        emit({"id": 2, "result": {"thread": thread}})
    time.sleep(30)
'''


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.binary = self.root / "codex"
        self.binary.write_text(f"#!{sys.executable}\n" + _FAKE)
        self.binary.chmod(0o700)
        self.own, self.parent = str(uuid.uuid4()), str(uuid.uuid4())
        self.env = {"CODEX_THREAD_ID": self.own, "MURMUR_CODEX_BINARY": str(self.binary),
                    "HOME": str(self.root), "CODEX_HOME": str(self.root / "config"),
                    "CODEX_SQLITE_HOME": str(self.root / "queue")}
        self.configure()

    def configure(self, mode="ok", **values):
        self.settings = {"mode": mode, "thread": {"id": self.own, "forkedFromId": self.parent}, **values}
        (self.root / "fixture.json").write_text(json.dumps(self.settings))

    def identity(self):
        return codex.current_identity({"session_id": self.own, "cwd": str(self.root)}, env=self.env)

    def rows(self, name="invocations.jsonl"):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_reaped(self):
        for row in self.rows():
            with self.assertRaises(ProcessLookupError):
                os.kill(row["pid"], 0)

    def source_identity(self, source, **changes):
        identity = self.identity()
        return replace(identity, evidence={**identity.evidence,
            "codex_thread_source": source, "codex_thread_source_session_id": self.own,
            "codex_thread_source_origin": "app-server:thread/read", **changes})

    def spawned_source(self):
        # Generated .166 ThreadReadResponse schema: API camel-A subAgent,
        # rather than the state database's lowercase subagent serialization.
        return {"subAgent": {"thread_spawn": {"parent_thread_id": self.parent, "depth": 1,
                    "agent_nickname": "Fixture", "agent_role": "worker", "agent_path": None}}}

    def test_fast_identity_uses_exact_invoking_env_or_hook_and_never_starts_rpc(self):
        with patch.object(codex.subprocess, "Popen", side_effect=AssertionError("hook ran RPC")):
            identity = self.identity()
            self.assertEqual(identity.provider, "codex")
            self.assertEqual(identity.session_id, self.own)
            self.assertFalse(identity.parent_known)
            self.assertIsNone(identity.parent_session_id)
            hook_only = codex.current_identity({"session_id": self.own}, env={})
            self.assertEqual(hook_only.session_id, self.own)
        self.assertNotIn("native_epoch", identity.target)
        self.assertIn("unobserved", identity.evidence["routing_scope"])
        json.dumps(identity.target)

    def test_ambiguous_runtime_or_mismatched_id_cannot_select_other_session(self):
        for key in codex._CLAUDE_ENV:
            with self.subTest(key=key), self.assertRaises(IdentityError):
                codex.current_identity(env={**self.env, key: "present"})
        with self.assertRaisesRegex(IdentityError, "disagree"):
            codex.current_identity({"session_id": self.parent}, env=self.env)
        for value in (None, "my-session", "../thread", self.own.upper(), " " + self.own):
            with self.subTest(value=value), self.assertRaises(IdentityError):
                codex.current_identity(env={**self.env, "CODEX_THREAD_ID": value})

    def test_read_only_rpc_uses_exact_route_and_proves_direct_parent(self):
        identity = self.identity()
        result = codex.resolve_identity(identity, timeout=1)
        self.assertEqual((result.session_id, result.parent_session_id, result.parent_known),
                         (self.own, self.parent, True))
        self.assertEqual(result.target, identity.target)
        invocation = self.rows()[0]
        self.assertEqual(invocation["argv"], ["app-server", "--stdio"])
        self.assertEqual((invocation["cwd"], invocation["home"], invocation["sqlite"]),
                         (str(self.root), self.env["CODEX_HOME"], self.env["CODEX_SQLITE_HOME"]))
        requests = self.rows("requests.jsonl")
        self.assertEqual([item["method"] for item in requests],
                         ["initialize", "initialized", "thread/read"] * 2)
        self.assertEqual(requests[2]["params"], {"threadId": self.own, "includeTurns": False})
        self.assertEqual(requests[-1]["params"], {"threadId": self.parent, "includeTurns": False})
        self.assertFalse(result.evidence["lineage_complete"])
        self.assertIn("different", result.evidence["lineage_reason"])
        self.assert_reaped()

    def test_bounded_hooks_list_uses_captured_cwd_profile_and_reaps_metadata_producer(self):
        hooks = {"data": [{"cwd": str(self.root), "hooks": []}]}
        self.configure(hooks=hooks)
        identity = self.identity()
        identity.target["cli_args"] = ["--profile", "fixture"]
        result, target = codex._read_only_request(identity, "hooks/list", {"cwds": [str(self.root)]}, timeout=1)
        self.assertEqual(result, hooks)
        self.assertEqual(target, identity.target)
        self.assertEqual(self.rows()[0]["argv"], ["--profile", "fixture", "app-server", "--stdio"])
        self.assertEqual((self.rows()[0]["cwd"], self.rows()[0]["home"], self.rows()[0]["sqlite"]),
                         (str(self.root), self.env["CODEX_HOME"], self.env["CODEX_SQLITE_HOME"]))
        requests = self.rows("requests.jsonl")
        self.assertEqual([item["method"] for item in requests], ["initialize", "initialized", "hooks/list"])
        self.assertEqual(requests[-1]["params"], {"cwds": [str(self.root)]})
        self.assertFalse((self.root / "submitted").exists())
        self.assert_reaped()

    def test_read_only_rpc_rejects_mutations_other_recipients_and_other_cwds_before_spawn(self):
        cases = (("thread/resume", {"threadId": self.own}), ("turn/start", {"threadId": self.own}),
                 ("thread/read", {"threadId": self.parent, "includeTurns": False}),
                 ("thread/read", {"threadId": self.own, "includeTurns": True}),
                 ("thread/read", {"threadId": self.own, "includeTurns": 0}),
                 ("hooks/list", {"cwds": ["/other"]}), ("hooks/list", {"cwds": []}),
                 ("hooks/list", {"cwds": [str(self.root)], "threadId": self.parent}))
        with patch.object(codex.subprocess, "Popen") as launch:
            for method, params in cases:
                with self.subTest(method=method, params=params), self.assertRaises(IdentityError):
                    codex._read_only_request(self.identity(), method, params, timeout=1)
        launch.assert_not_called()

    def test_verified_thread_read_retains_own_spawn_source_without_guessing_fork_parent(self):
        source = self.spawned_source()
        self.configure(thread={"id": self.own, "forkedFromId": None, "source": source})
        identity = codex.resolve_identity(self.identity(), timeout=1)
        self.assertEqual(codex.verified_thread_source(identity), source)
        self.assertEqual(identity.evidence["codex_thread_source_session_id"], self.own)
        self.assertEqual(identity.evidence["codex_thread_source_origin"], "app-server:thread/read")
        self.assertIsNone(identity.parent_session_id)
        self.assertIn("manual messaging", codex.automatic_enrollment_reason(identity))
        self.assertEqual([row["method"] for row in self.rows("requests.jsonl")],
                         ["initialize", "initialized", "thread/read"])
        self.assert_reaped()

    def test_ordinary_fork_does_not_inherit_ancestors_spawn_source(self):
        self.configure(thread={"id": self.own, "forkedFromId": self.parent, "source": "appServer"},
                       threads={self.parent: {"id": self.parent, "forkedFromId": None,
                                              "source": self.spawned_source()}})
        identity = codex.resolve_identity(self.identity(), timeout=1)
        self.assertEqual(identity.parent_session_id, self.parent)
        self.assertEqual(codex.verified_thread_source(identity), "appServer")
        self.assertIsNone(codex.automatic_enrollment_reason(identity))
        self.assert_reaped()

    def test_missing_fresh_source_clears_previous_source_instead_of_reusing_it(self):
        previous = self.source_identity(self.spawned_source())
        self.configure(thread={"id": self.own, "forkedFromId": None})
        current = codex.resolve_identity(previous, timeout=1)
        self.assertIsNone(codex.verified_thread_source(current))
        self.assertIsNone(codex.automatic_enrollment_reason(current))
        self.assert_reaped()

    def test_spawn_guard_requires_exact_verified_source_shape_not_labels_or_ancestry(self):
        malformed = [None, "unknown", "appServer", "subagent.thread_spawn",
            {"custom": "thread_spawn"}, {"subAgent": "review"}, {"subAgent": "compact"},
            {"subagent": self.spawned_source()["subAgent"]},
            {"subAgent": {"thread_spawn": {"parent_thread_id": self.parent, "depth": True}}},
            {"subAgent": {"thread_spawn": {"parent_thread_id": "parent title", "depth": 1}}},
            {"subAgent": {"thread_spawn": {"parent_thread_id": self.parent}}},
            {**self.spawned_source(), "other": "extra union tag"}]
        for source in malformed:
            with self.subTest(source=source):
                identity = replace(self.source_identity(source), parent_session_id=self.parent, parent_known=True)
                self.assertIsNone(codex.automatic_enrollment_reason(identity))
        for changes in ({"codex_thread_source_origin": "environment"},
                        {"codex_thread_source_session_id": self.parent}):
            self.assertIsNone(codex.automatic_enrollment_reason(self.source_identity(self.spawned_source(), **changes)))
        self.assertIsNone(codex.automatic_enrollment_reason(replace(self.identity(), evidence={
            "title": "spawned worker", "parent_source": "app-server:thread/read"})))
        self.assertEqual(self.rows(), [])

    def test_known_spawned_subagent_refuses_target_and_queue_without_native_effects(self):
        identity = self.source_identity(self.spawned_source())
        identity = replace(identity, target={**identity.target, "binary": "/stale/owner/codex"})
        with patch.object(codex.subprocess, "Popen") as launch, patch.object(codex.desktop_wake, "verify") as desktop:
            with self.assertRaisesRegex(IdentityError, "Spawned Codex subagents"):
                codex.verify_target(identity)
            result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
        self.assertEqual(result.status, "rejected", result)
        self.assertIn("manual messaging", result.detail)
        launch.assert_not_called()
        desktop.assert_not_called()
        self.assertFalse((self.root / "submitted").exists())

    def test_exact_ancestor_metadata_keeps_all_edges_without_loading_threads(self):
        root = str(uuid.uuid4())
        self.configure(threads={self.parent: {"id": self.parent, "forkedFromId": root},
                                root: {"id": root, "forkedFromId": None}})
        result = codex.resolve_identity(self.identity(), timeout=1)
        self.assertTrue(result.evidence["lineage_complete"])
        self.assertEqual(result.evidence["lineage_edges"], [
            {"session_id": self.own, "parent_session_id": self.parent},
            {"session_id": self.parent, "parent_session_id": root},
            {"session_id": root, "parent_session_id": None}])
        self.assertEqual([row["params"]["threadId"] for row in self.rows("requests.jsonl")
                          if row["method"] == "thread/read"], [self.own, self.parent, root])
        self.assert_reaped()

    def test_ancestor_cycles_reject_and_missing_ancestor_parent_never_becomes_root(self):
        self.configure(threads={self.parent: {"id": self.parent, "forkedFromId": self.own}})
        with self.assertRaisesRegex(IdentityError, "cycle"):
            codex.resolve_identity(self.identity(), timeout=1)
        self.configure(threads={self.parent: {"id": self.parent}})
        result = codex.resolve_identity(self.identity(), timeout=1)
        self.assertFalse(result.evidence["lineage_complete"])
        self.assertEqual(result.evidence["lineage_edges"], [
            {"session_id": self.own, "parent_session_id": self.parent}])
        self.assert_reaped()

    def test_null_parent_is_explicit_root_but_omitted_or_invalid_parent_is_not(self):
        self.configure(thread={"id": self.own, "forkedFromId": None})
        result = codex.resolve_identity(self.identity(), timeout=1)
        self.assertTrue(result.parent_known)
        self.assertIsNone(result.parent_session_id)
        for thread in ({"id": self.own}, {"id": self.parent, "forkedFromId": None},
                       {"id": self.own, "forkedFromId": self.own},
                       {"id": self.own, "forkedFromId": "parent-name"}):
            self.configure(thread=thread)
            with self.subTest(thread=thread), self.assertRaises(IdentityError):
                codex.resolve_identity(self.identity(), timeout=1)
        self.assert_reaped()

    def test_malformed_closed_failed_and_out_of_order_rpc_are_reaped(self):
        for mode in ("malformed", "closed", "early", "error", "array"):
            self.configure(mode)
            with self.subTest(mode=mode), self.assertRaises(IdentityError):
                codex.resolve_identity(self.identity(), timeout=1)
        self.assert_reaped()

    def test_timeout_kills_and_reaps_uncooperative_metadata_process(self):
        self.configure("timeout")
        started = time.monotonic()
        with self.assertRaisesRegex(IdentityError, "timed out"):
            codex.resolve_identity(self.identity(), timeout=0.15)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_reaped()

    def test_line_and_total_output_bounds_apply_before_eof(self):
        for mode in ("line", "total"):
            self.configure(mode, size=5000 if mode == "line" else 100)
            with self.subTest(mode=mode), patch.object(codex, "_MAX_LINE", 1024), patch.object(
                    codex, "_MAX_OUTPUT", 65536 if mode == "line" else 4096), self.assertRaisesRegex(
                        IdentityError, "line bound" if mode == "line" else "total output bound"):
                codex.resolve_identity(self.identity(), timeout=1)
        self.assert_reaped()

    def test_ancestor_metadata_accepts_only_exact_id_and_explicit_route(self):
        result = codex.metadata(self.own, env=self.env, executable=str(self.binary), timeout=1)
        self.assertEqual(result.parent_session_id, self.parent)
        with self.assertRaises(IdentityError):
            codex.metadata("parent label", env=self.env, timeout=1)
        with self.assertRaisesRegex(IdentityError, "cannot be replaced"):
            codex.resolve_identity(self.identity(), env=self.env, timeout=1)

    def test_target_verification_checks_only_local_route_and_preserves_identity(self):
        identity = self.identity()
        with patch.object(codex.subprocess, "Popen", side_effect=AssertionError("verification ran RPC")):
            self.assertIs(codex.verify_target(identity), identity)
            for change in ({"binary": "/missing/codex"}, {"cwd": "/missing/workspace"},
                           {"env": {}}, {"cli_args": ["--thread", self.parent]},
                           {"binary": "codex"}):
                target = {**identity.target, **change}
                with self.subTest(change=change), self.assertRaises(IdentityError):
                    codex.verify_target(NativeIdentity("codex", self.own, target=target))

    def test_deliver_keeps_exact_argv_text_and_captured_route_even_after_env_changes(self):
        identity = self.identity()
        text, token = "You have new messages; $(touch /tmp/no-shell) 'quoted' [wake:abc_123]", "abc_123"
        with patch.dict(os.environ, {"CODEX_HOME": "wrong", "CODEX_SQLITE_HOME": "wrong"}), patch.object(
                codex.desktop_wake, "wake", side_effect=AssertionError("CLI delivery must not wake Desktop")):
            result = codex.deliver(identity, text, token, timeout=1)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.target, identity.target)
        invocation = self.rows()[0]
        self.assertEqual(invocation["argv"], ["queue", "--thread", self.own, "--message", text])
        self.assertEqual(invocation["home"], self.env["CODEX_HOME"])
        self.assertEqual(invocation["sqlite"], self.env["CODEX_SQLITE_HOME"])
        self.assert_reaped()

    def test_generic_nonzero_after_a_real_side_effect_is_uncertain_not_safe_to_retry(self):
        self.configure(exit=1)
        result = codex.deliver(self.identity(), "Murmur: inbox [wake:x]", "x", timeout=1)
        self.assertTrue((self.root / "submitted").is_file())
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(len(self.rows()), 1)
        self.assert_reaped()

    def test_desktop_capture_is_evidence_only_and_cli_absence_is_explicit(self):
        ordinary = self.identity()
        descriptor = {"schema": 1, "app": "synthetic desktop"}
        with patch.object(codex.desktop_wake, "capture", return_value=descriptor), patch.object(
                codex.desktop_wake, "verify") as verify:
            desktop = self.identity()
            self.assertEqual(desktop.target, ordinary.target)
            self.assertEqual(desktop.evidence["desktop_wake"], descriptor)
            verify.assert_not_called()
            self.assertIs(codex.verify_target(desktop), desktop)
            verify.assert_called_once_with(desktop, timeout=3)
        self.assertIsNone(ordinary.evidence["desktop_wake"])
        self.assertEqual(self.rows(), [])

    def test_desktop_enqueues_once_before_one_wake_only_handoff(self):
        descriptor = {"schema": 1, "app": "synthetic desktop"}
        identity = replace(self.identity(), evidence={"desktop_wake": descriptor})

        def wake(session, captured, *, timeout):
            self.assertTrue((self.root / "submitted").is_file())
            self.assertEqual((session, captured, timeout), (self.own, descriptor, 1))
            self.assertEqual(len(self.rows()), 1)
            return DeliveryResult("accepted", "Background wake accepted; admission unconfirmed", {"wake": True})

        with patch.object(codex.desktop_wake, "verify", return_value=descriptor) as verify, patch.object(
                codex.desktop_wake, "wake", side_effect=wake) as dispatch:
            result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
        self.assertEqual(result.status, "accepted")
        verify.assert_called_once_with(identity, timeout=1)
        dispatch.assert_called_once()
        self.assertEqual(self.rows()[0]["argv"],
                         ["queue", "--thread", self.own, "--message", "Murmur: inbox [wake:x]"])
        self.assert_reaped()

    def test_desktop_wake_failure_after_enqueue_never_becomes_safe_rejection(self):
        identity = replace(self.identity(), evidence={"desktop_wake": {"schema": 1}})
        outcomes = [DeliveryResult("uncertain", "lost response"),
                    DeliveryResult("rejected", "owner changed"), OSError("open failed")]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), patch.object(codex.desktop_wake, "verify"), patch.object(
                    codex.desktop_wake, "wake", **({"side_effect": outcome} if isinstance(outcome, Exception)
                                                  else {"return_value": outcome})) as dispatch:
                before = len(self.rows())
                result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
                self.assertEqual(result.status, "uncertain")
                self.assertEqual(len(self.rows()), before + 1)
                dispatch.assert_called_once()
        self.assert_reaped()

    def test_desktop_preflight_failure_cannot_enqueue_or_fall_back(self):
        identity = replace(self.identity(), evidence={"desktop_wake": {"schema": 1}})
        with patch.object(codex.desktop_wake, "verify", side_effect=IdentityError("wrong queue realm")), patch.object(
                codex.desktop_wake, "wake") as wake:
            result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.rows(), [])
        wake.assert_not_called()

    def test_uncertain_desktop_enqueue_does_not_wake_another_queued_item(self):
        self.configure(exit=1)
        identity = replace(self.identity(), evidence={"desktop_wake": {"schema": 1}})
        with patch.object(codex.desktop_wake, "verify"), patch.object(codex.desktop_wake, "wake") as wake:
            result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
        self.assertEqual(result.status, "uncertain")
        self.assertTrue((self.root / "submitted").exists())
        self.assertEqual(len(self.rows()), 1)
        wake.assert_not_called()
        self.assert_reaped()

    def test_explicit_config_arguments_and_absent_environment_are_preserved(self):
        identity = self.identity()
        identity.target["cli_args"] = ["--profile", "test", "-c", 'sqlite_home="/explicit/queue"']
        identity.target["env"]["CODEX_SQLITE_HOME"] = None
        with patch.dict(os.environ, {"CODEX_SQLITE_HOME": "unrelated-service-queue"}):
            result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(self.rows()[0]["argv"][:4],
                         ["--profile", "test", "-c", 'sqlite_home="/explicit/queue"'])
        self.assertIsNone(self.rows()[0]["sqlite"])
        self.assert_reaped()

    def test_option_shaped_config_values_reject_without_dispatch(self):
        for option in ("--profile", "-p", "--config", "-c"):
            identity = self.identity()
            identity.target["cli_args"] = [option, "--help"]
            with self.subTest(option=option), patch.object(
                    codex.subprocess, "Popen", side_effect=AssertionError("invalid route dispatched")):
                result = codex.deliver(identity, "Murmur: inbox [wake:x]", "x", timeout=1)
                self.assertEqual(result.status, "rejected")
                self.assertIn("option prefix", result.detail)
        self.assertEqual(self.rows(), [])

    def test_queue_timeout_is_uncertain_and_process_is_reaped(self):
        self.configure("timeout")
        result = codex.deliver(self.identity(), "Murmur: inbox [wake:x]", "x", timeout=0.15)
        self.assertEqual(result.status, "uncertain")
        self.assertIsNotNone(result.target)
        self.assert_reaped()

    def test_invalid_doorbell_missing_binary_and_spawn_failure_reject_before_attempt(self):
        identity = self.identity()
        for text, token in (("Murmur: inbox", "x"), ("Murmur: [wake:y]", "x"),
                            ("Murmur: [wake:x] [wake:x]", "x"), ("Murmur: [wake:x]", "../x")):
            with self.subTest(text=text, token=token):
                self.assertEqual(codex.deliver(identity, text, token).status, "rejected")
        self.binary.unlink()
        self.assertEqual(codex.deliver(identity, "Murmur: inbox [wake:x]", "x").status, "rejected")
        self.assertEqual(self.rows(), [])
        self.binary.write_text("#!/missing/interpreter\n")
        self.binary.chmod(0o700)
        self.assertEqual(codex.deliver(identity, "Murmur: inbox [wake:x]", "x").status, "rejected")
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
