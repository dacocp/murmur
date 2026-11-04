"""Recipient-scoped capability facts; no installed processes or messages."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from murmur import native
from murmur.native import capabilities, context_receipts, process
from murmur.native.types import NativeError, NativeIdentity


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SHA_0163 = "1afb28c01d7d49e892bd75f11076b10f1451e34e0ee308a95e5ab7f69f9c84fe"
SHA_0164 = "d8990e3ee7acfbc26f91265b12a1a83f0fee40d0e0f8f1f40a37d4ec21f499cf"
SHA_LINUX_0164 = "b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306"
SHA_ALPHA11 = "a04752e4d46a4d89e53e6f61c59c5349a638c8ce172ffbf391fd482b4d902a4e"
SHA_0165 = "d84a54b031085f01e3ad915ea7c72ebea4dcf69e90f0d1e0e4a637e8280991d2"


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "sessions").mkdir()
        self.history = self.root / "sessions" / "rollout.jsonl"
        self.identity = NativeIdentity("codex", SESSION, target={
            "binary": "/recipient/codex", "cwd": "/recipient/project", "transport": "queue", "cli_args": [],
            "env": {"HOME": str(self.root), "CODEX_HOME": str(self.root), "CODEX_SQLITE_HOME": None}})
        self.owner = {"pid": 123, "uid": os.getuid(), "birth": "fixture birth",
                      "executable": "/actual-owner/codex", "sha256": SHA_0163,
                      "version": "0.163.0-alpha.10.2"}
        current = patch.object(native, "current_identity", return_value=self.identity)
        inspect = patch.object(context_receipts, "inspect_context_owner", return_value=self.owner)
        self.current = current.start()
        self.inspect = inspect.start()
        self.addCleanup(current.stop)
        self.addCleanup(inspect.stop)

    def report(self, **options):
        return capabilities.route_capabilities(self.identity, self.identity, **options)

    def write_history(self, **changes):
        header = {"id": SESSION, "session_id": SESSION, "cli_version": self.owner["version"],
                  "history_mode": "paginated", **changes}
        self.history.write_text(json.dumps({"ordinal": 4871, "type": "session_meta", "payload": header}) + "\n")
        self.history.chmod(0o600)

    def source_identity(self, source, **changes):
        return replace(self.identity, evidence={**self.identity.evidence,
            "codex_thread_source": source, "codex_thread_source_session_id": SESSION,
            "codex_thread_source_origin": "app-server:thread/read", **changes})

    def test_verified_spawn_route_is_unsupported_before_stale_owner_gate_but_manual_stays_disabled(self):
        source = {"subAgent": {"thread_spawn": {"parent_thread_id": OTHER, "depth": 1}}}
        identity = self.source_identity(source)
        for verified in (None, identity):
            with self.subTest(verified=verified):
                result = capabilities.route_capabilities(identity, verified)
                self.assertEqual({state["status"] for state in result.values()}, {"unsupported"})
                self.assertEqual(result["idle_queue"]["evidence"]["thread_source"], source)
                self.assertIn("manual messaging", result["idle_queue"]["reason"])
        manual = capabilities.route_capabilities(identity, mode="manual")
        self.assertEqual({state["status"] for state in manual.values()}, {"disabled"})
        self.current.assert_not_called()
        self.inspect.assert_not_called()

    def test_unknown_source_does_not_borrow_desktop_thread_qualification(self):
        for source in (None, "unknown", {"custom": "worker"}, {"subagent": {"thread_spawn": {}}}):
            identity = self.source_identity(source)
            identity = replace(identity, evidence={**identity.evidence, "desktop_wake": {"schema": 1}})
            result = capabilities.route_capabilities(identity, identity)
            with self.subTest(source=source):
                self.assertEqual(result["idle_queue"]["status"], "unverified")
                self.assertEqual(result["idle_queue"]["evidence"]["thread_source_status"], "unverified")
        source = {"subAgent": {"thread_spawn": {"parent_thread_id": OTHER, "depth": 1}}}
        for changes in ({"codex_thread_source_session_id": OTHER}, {"codex_thread_source_origin": "environment"}):
            identity = self.source_identity(source, **changes)
            result = capabilities.route_capabilities(identity, identity)
            self.assertNotIn("unsupported", {state["status"] for state in result.values()})

    def test_verified_ordinary_fork_source_preserves_desktop_route_support(self):
        for source in ("cli", "vscode", "exec", "appServer"):
            identity = self.source_identity(source)
            identity = replace(identity, parent_known=True, parent_session_id=OTHER,
                               evidence={**identity.evidence, "desktop_wake": {"schema": 1}})
            with self.subTest(source=source):
                result = capabilities.route_capabilities(identity, identity)
                self.assertEqual(result["idle_queue"]["status"], "supported")
                self.assertEqual(result["idle_queue"]["evidence"]["thread_source_status"], "verified")

    def test_manual_enrollment_performs_no_native_inspection(self):
        result = self.report(mode="manual")
        self.assertEqual({route: state["status"] for route, state in result.items()},
                         {"idle_queue": "disabled", "busy_context": "disabled"})
        self.current.assert_not_called()
        self.inspect.assert_not_called()

    def test_failed_missing_or_other_recipient_verification_never_uses_invoking_owner(self):
        for verified in (None, False, replace(self.identity, session_id=OTHER), replace(self.identity, target={})):
            with self.subTest(verified=verified):
                result = capabilities.route_capabilities(self.identity, verified)
                self.assertEqual([state["status"] for state in result.values()], ["unverified", "unverified"])
        self.current.assert_not_called()
        self.inspect.assert_not_called()

    def test_context_support_requires_exact_invoking_actor_and_route(self):
        changed = []
        for field, value in (("binary", "/daemon/codex"), ("cwd", "/other/project"),
                             ("env", {"HOME": "/other"}), ("cli_args", ["-p", "other"])):
            changed.append(replace(self.identity, target={**self.identity.target, field: value}))
        for invoking in (replace(self.identity, session_id=OTHER), *changed):
            with self.subTest(invoking=invoking):
                self.current.return_value = invoking
                result = self.report()
                self.assertEqual(result["busy_context"]["status"], "unverified")
                self.assertNotIn("owner", result["busy_context"].get("evidence", {}))
        self.inspect.assert_not_called()

    def test_desktop_idle_support_uses_verified_recipient_not_an_invoking_hint(self):
        source = self.source_identity("appServer")
        hint = replace(source, evidence={**source.evidence, "desktop_wake": {"schema": 1}})
        ordinary = capabilities.route_capabilities(hint, self.identity)
        self.assertEqual(ordinary["idle_queue"]["status"], "unverified")
        verified = capabilities.route_capabilities(self.identity, hint)
        self.assertEqual(verified["idle_queue"]["status"], "supported")
        self.assertEqual(verified["idle_queue"]["evidence"]["transport"], "queue+desktop-wake")
        self.assertIn("admission is unconfirmed", verified["idle_queue"]["reason"])

    def test_certified_owner_without_history_is_unverified_and_never_claims_queue_admission(self):
        for sha, version in ((SHA_0163, "0.163.0-alpha.10.2"), (SHA_0164, "0.164.0"),
                             (SHA_ALPHA11, "0.164.0-alpha.11.1"), (SHA_LINUX_0164, "0.164.0"),
                             (SHA_0165, "0.165.0-alpha.1.2")):
            self.owner.update(sha256=sha, version=version)
            with self.subTest(version=version):
                result = self.report(timeout=0.25)
                self.assertEqual(result["busy_context"]["status"], "unverified")
                self.assertEqual(result["busy_context"]["evidence"]["owner"], self.owner)
                self.assertEqual(result["busy_context"]["evidence"]["profile_status"], "supported")
                self.assertEqual(result["busy_context"]["evidence"]["history"]["status"], "unverified")
                self.assertIn("header is not checked", result["busy_context"]["reason"])
                self.assertEqual(result["idle_queue"]["status"], "unverified")
                self.assertEqual(result["idle_queue"]["evidence"], {
                    "transport": "queue", "local_target_verified": True, "startup_overrides_verified": False,
                    "thread_source_status": "unverified"})
                self.assertGreater(self.inspect.call_args.kwargs["timeout"], 0)
                self.assertLessEqual(self.inspect.call_args.kwargs["timeout"], 0.25)

    def test_matching_exact_header_supports_route_but_not_capture_or_admission(self):
        self.write_history()
        before = self.history.read_bytes(), self.history.stat().st_mtime_ns
        result = self.report(transcript_path=str(self.history))
        self.assertEqual(result["busy_context"]["status"], "supported")
        history = result["busy_context"]["evidence"]["history"]
        self.assertEqual(history["status"], "supported")
        self.assertEqual(history["evidence"]["header_version"], self.owner["version"])
        self.assertIn("capture and admission remain separate", result["busy_context"]["reason"])
        self.assertEqual((self.history.read_bytes(), self.history.stat().st_mtime_ns), before)

    def test_immutable_resumed_header_mismatch_is_visible_despite_certified_owner(self):
        self.owner.update(sha256=SHA_ALPHA11, version="0.164.0-alpha.11.1")
        self.write_history(cli_version="0.163.0-alpha.10.2")
        before = self.history.read_bytes()
        result = self.report(transcript_path=str(self.history))
        self.assertEqual(result["busy_context"]["status"], "unsupported")
        self.assertEqual(result["busy_context"]["evidence"]["profile_status"], "supported")
        observed = result["busy_context"]["evidence"]["history"]["evidence"]
        self.assertEqual((observed["owner_version"], observed["header_version"]),
                         ("0.164.0-alpha.11.1", "0.163.0-alpha.10.2"))
        self.assertEqual(self.history.read_bytes(), before)

    def test_0165_reports_owner_and_compatible_creation_header_without_claiming_admission(self):
        self.owner.update(sha256=SHA_0165, version="0.165.0-alpha.1.2")
        for creation, expected in (("0.165.0-alpha.1.2", "supported"),
                                   ("0.164.0-alpha.11.1", "supported"),
                                   ("0.163.0-alpha.10.2", "unsupported"),
                                   ("0.164.0", "unsupported")):
            self.write_history(cli_version=creation)
            before = self.history.read_bytes(), self.history.stat().st_mtime_ns
            with self.subTest(creation=creation):
                result = self.report(transcript_path=str(self.history))["busy_context"]
                self.assertEqual(result["status"], expected, result)
                observed = result["evidence"]["history"]["evidence"]
                self.assertEqual((observed["owner_version"], observed["header_version"]),
                                 ("0.165.0-alpha.1.2", creation))
                if expected == "supported":
                    self.assertIn("capture and admission remain separate", result["reason"])
                self.assertEqual((self.history.read_bytes(), self.history.stat().st_mtime_ns), before)

    def test_wrong_recipient_header_and_unavailable_or_unsafe_history_cannot_be_ready(self):
        self.write_history(session_id=OTHER)
        self.assertEqual(self.report(transcript_path=str(self.history))["busy_context"]["status"], "unsupported")
        self.history.unlink()
        self.assertEqual(self.report(transcript_path=str(self.history))["busy_context"]["status"], "unverified")
        self.write_history()
        self.history.chmod(0o666)
        self.assertEqual(self.report(transcript_path=str(self.history))["busy_context"]["status"], "unverified")

    def test_history_deadline_is_unverified_instead_of_incompatible(self):
        self.write_history()
        expired = False

        def expire(*_args):
            nonlocal expired
            expired = True
            raise NativeError("Native context inspection exceeded its deadline")

        with patch.object(context_receipts.time, "monotonic", side_effect=lambda: 2 if expired else 1), \
                patch.object(context_receipts, "_header", side_effect=expire):
            result = self.report(transcript_path=str(self.history))
        self.assertEqual(result["busy_context"]["status"], "unverified")
        self.assertIn("deadline", result["busy_context"]["reason"])

    def test_unknown_hash_or_mismatched_version_is_unsupported_even_with_version_text(self):
        for sha, version in (("f" * 64, "0.164.0"), ("f" * 64, "0.164.0-alpha.11.1"),
                             (SHA_LINUX_0164, "0.163.0-alpha.10.2"),
                             (SHA_0164, "0.163.0-alpha.10.2"),
                             (SHA_0165, "0.164.0-alpha.11.1"), ("f" * 64, "0.165.0-alpha.1.2")):
            self.owner.update(sha256=sha, version=version)
            with self.subTest(sha=sha, version=version):
                result = self.report()
                self.assertEqual(result["busy_context"]["status"], "unsupported")
                self.assertEqual(result["busy_context"]["evidence"]["owner"]["sha256"], sha)

    def test_inspection_failure_is_unverified_instead_of_unsupported_or_ready(self):
        self.current.side_effect = NativeError("Current identity is ambiguous")
        self.assertEqual(self.report()["busy_context"]["status"], "unverified")
        self.inspect.assert_not_called()
        self.current.side_effect = None
        for error in (NativeError("Owner changed"), OSError("Permission denied"), RuntimeError("Deadline")):
            with self.subTest(error=error):
                self.inspect.side_effect = error
                result = self.report()
                self.assertEqual(result["busy_context"]["status"], "unverified")
                self.assertIn(str(error), result["busy_context"]["reason"])

    def test_linux_claude_context_dispatch_keeps_native_history_and_admission_separate(self):
        identity = NativeIdentity("claude", SESSION, target={
            "platform": "linux", "version": "2.1.311", "runtime": "cli",
            "entrypoint": "cli", "socket_path": "/native/claude.sock",
            "native_epoch": "verified Linux epoch"})
        with patch("murmur.native.claude_context_receipts.inspect_context_history") as inspect:
            for path, state in ((None, {"status": "unverified", "reason": "No trusted history"}),
                                ("/native/history.jsonl", {"status": "supported", "reason": "History verified; admission is separate"})):
                inspect.return_value = state
                result = capabilities.route_capabilities(identity, identity, transcript_path=path, timeout=.25)
                self.assertEqual(result["busy_context"], state)
                self.assertEqual(result["idle_queue"]["status"], "supported")
                inspect.assert_called_with(identity, path, timeout=.25)
        self.inspect.assert_not_called()  # No substitution of a Codex owner.

    def test_linux_claude_requires_verified_recipient_before_history_inspection(self):
        identity = NativeIdentity("claude", SESSION, target={
            "platform": "linux", "version": "2.1.311", "runtime": "cli"})
        with patch("murmur.native.claude_context_receipts.inspect_context_history") as inspect:
            for verified in (None, replace(identity, session_id=OTHER), replace(identity, target={})):
                result = capabilities.route_capabilities(identity, verified)
                self.assertEqual(result["busy_context"]["status"], "unverified")
                self.assertEqual(result["idle_queue"]["status"], "unverified")
            inspect.assert_not_called()

    def test_other_claude_profiles_never_borrow_linux_context_support(self):
        base = {"runtime": "cli", "socket_path": "/native/claude.sock", "native_epoch": "epoch"}
        with patch("murmur.native.claude_context_receipts.inspect_context_history") as inspect:
            for profile in ({"platform": "darwin", "version": "2.1.310"},
                            {"platform": "linux", "version": "2.1.310"},
                            {"platform": "linux", "version": "2.1.312"},
                            {"platform": "linux", "version": "2.1.311", "runtime": "desktop"}):
                identity = NativeIdentity("claude", SESSION, target={**base, **profile})
                with self.subTest(profile=profile):
                    result = capabilities.route_capabilities(identity, identity)
                    self.assertNotEqual(result["busy_context"]["status"], "supported")
            inspect.assert_not_called()

    def test_claude_idle_profile_is_independent_of_unsupported_busy_route(self):
        for runtime, session in (("cli", SESSION), ("desktop", "local_" + SESSION)):
            identity = NativeIdentity("claude", session, target={
                "runtime": runtime, "socket_path": "/native/claude.sock", "native_epoch": "verified epoch"})
            with self.subTest(runtime=runtime):
                result = capabilities.route_capabilities(identity, identity)
                self.assertEqual(result["idle_queue"]["status"], "supported")
                self.assertEqual(result["idle_queue"]["evidence"]["transport"], "socket")
                self.assertEqual(result["busy_context"]["status"], "unverified" if runtime == "desktop" else "unsupported")
                result = capabilities.route_capabilities(identity)
                self.assertEqual(result["idle_queue"]["status"], "unverified")
                self.assertEqual(result["busy_context"]["status"], "unsupported")
        self.current.assert_not_called()
        self.inspect.assert_not_called()

    def test_claude_without_idle_socket_stays_unverified(self):
        identity = NativeIdentity("claude", SESSION, target={"runtime": "cli"})
        self.assertEqual(capabilities.route_capabilities(identity, identity)["idle_queue"]["status"], "unverified")

    def test_desktop_claude_peer_route_uses_its_recipient_profile_not_linux_or_codex(self):
        identity = NativeIdentity("claude", "local_" + SESSION, target={
            "runtime": "desktop", "entrypoint": "claude-desktop", "socket_path": "/native/claude.sock", "native_epoch": "epoch"})
        state = {"status": "supported", "reason": "Measured peer profile; admission is separate"}
        with patch("murmur.native.claude_peer_receipts.inspect_peer_history", return_value=state) as inspect:
            result = capabilities.route_capabilities(identity, identity, timeout=.25)
            self.assertEqual(result["busy_context"], state)
            self.assertEqual(result["idle_queue"]["status"], "supported")
            inspect.assert_called_once_with(identity, None, timeout=.25)
        self.current.assert_not_called()
        self.inspect.assert_not_called()


class OwnerInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "codex"
        self.path.write_bytes(b"synthetic owner executable")
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        parent = patch.object(context_receipts.os, "getppid", return_value=123)
        identity = patch.object(process, "identity", return_value=(100, os.getuid(), "owner birth"))
        inspect = patch.object(process, "inspect", return_value=(str(self.path), (str(self.path), "app-server")))
        self.parent, self.identity, self.inspect = parent.start(), identity.start(), inspect.start()
        for mock in (parent, identity, inspect):
            self.addCleanup(mock.stop)

    def test_measure_unknown_owner_but_capture_remains_fail_closed_without_outer_fallback(self):
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        facts = context_receipts.inspect_context_owner()
        self.assertEqual(facts, {"pid": 123, "uid": os.getuid(), "birth": "owner birth",
                                 "executable": str(self.path), "sha256": self.sha, "version": None})
        self.assertIsNone(context_receipts.context_profile_version(self.sha))
        self.assertEqual(self.inspect.call_args_list[0].args, (123,))
        with self.assertRaisesRegex(NativeError, "Unsupported native Codex context profile"):
            context_receipts._owner_profile(time.monotonic() + 0.5)
        self.assertTrue(all(call.args == (123,) for call in self.inspect.call_args_list))
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_capture_keeps_identical_certified_profile_after_refactor(self):
        with patch.dict(context_receipts._PROFILES, {self.sha: "fixture version"}):
            measured = context_receipts.inspect_context_owner()
            certified = context_receipts._owner_profile(time.monotonic() + 0.5)
        self.assertEqual(measured, certified)
        self.assertEqual(certified["version"], "fixture version")

    def test_changed_owner_wrong_user_or_ambiguous_argv_never_certifies(self):
        before = (100, os.getuid(), "owner birth")
        for first, last, argv in ((before, (100, os.getuid(), "new birth"), (str(self.path),)),
                                  ((100, os.getuid() + 1, "owner birth"), before, (str(self.path),)),
                                  (before, before, ("ambiguous", "app-server"))):
            with self.subTest(first=first, last=last, argv=argv):
                self.identity.side_effect = [first, last]
                self.inspect.return_value = (str(self.path), argv)
                with self.assertRaises(NativeError):
                    context_receipts.inspect_context_owner()

    def test_deadline_and_invalid_timeouts_perform_no_process_inspection(self):
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(NativeError):
                context_receipts.inspect_context_owner(timeout=timeout)
        with patch.object(context_receipts.time, "monotonic", side_effect=[1, 2]):
            with self.assertRaisesRegex(NativeError, "deadline"):
                context_receipts.inspect_context_owner(timeout=0.5)
        self.identity.assert_not_called()
        self.inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
