"""Receipts follow native prompt/tool/result/context ancestry, not quoted text."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch
import uuid

from murmur import native
from murmur.native import claude_context_receipts as receipts
from murmur.native.types import HookEvent, NativeError, NativeIdentity


class ClaudeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.session, self.prompt = str(uuid.uuid4()), str(uuid.uuid4())
        self.tool_id, self.text = "toolu_actual", "You have new messages. [wake:opaque]"
        config = self.root / ".claude"
        self.path = config / "projects/work" / (self.session + ".jsonl")
        self.path.parent.mkdir(parents=True)
        self.identity = NativeIdentity("claude", self.session, target={
            "runtime": "cli", "entrypoint": "cli", "platform": "linux", "version": "2.1.311",
            "config_dir": str(config), "native_epoch": "exact-process-epoch"})
        self.user = self.node("user", None, promptId=self.prompt, promptSource="typed",
                              message={"role": "user", "content": "Original work"})
        self.tool = self.node("assistant", self.user["uuid"], message={"role": "assistant",
            "content": [{"type": "tool_use", "id": self.tool_id, "name": "Bash", "input": {}}]})
        self.baseline = [self.user, self.tool]
        self.write(self.baseline)
        self.captured = HookEvent(self.identity, "post_tool", "PostToolUse",
            "claude:" + self.session + ":" + self.prompt, tool_id=self.tool_id,
            transcript_path=str(self.path))
        self.owner = patch.object(receipts.claude, "current_identity", return_value=self.identity)
        self.owner.start()
        self.addCleanup(self.owner.stop)
        platform = patch.object(receipts.claude, "_platform", return_value="linux")
        platform.start()
        self.addCleanup(platform.stop)
        self.request = receipts.prepare_context_receipt(self.captured)
        self.attempt = {"actor": self.identity.actor, "token": "opaque", "tool_id": self.tool_id,
                        "expected_turn_key": self.captured.turn_key,
                        "receipt_request": self.request, "emission": {"context": self.text}}
        self.result = self.node("user", self.tool["uuid"], message={"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": self.tool_id, "content": "Original output"}]})
        self.success = self.node("attachment", self.result["uuid"], attachment={"type": "hook_success"})
        self.context = self.node("attachment", self.success["uuid"], attachment={
            "type": "hook_additional_context", "hookEvent": "PostToolUse", "hookName": "PostToolUse:Bash",
            "toolUseID": self.tool_id, "content": [self.text]}, renderedRole="system",
            rendered=[{"content": "<system-reminder>\nPostToolUse:Bash hook additional context: "
                       + self.text + "\n</system-reminder>"}])

    def node(self, kind, parent, **fields):
        return {"type": kind, "uuid": str(uuid.uuid4()), "parentUuid": parent,
                "isSidechain": False, "sessionId": self.session, "version": "2.1.311", **fields}

    def write(self, rows):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.path.chmod(0o600)

    def append(self, *rows):
        with self.path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def verify(self):
        return receipts.verify_context_receipt(self.identity, self.attempt)

    def test_exact_native_attachment_confirms_only_its_captured_prompt_and_tool(self):
        self.assertEqual(self.verify()["status"], "unknown")
        self.append(self.result, self.success, self.context)
        result = self.verify()
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(result["evidence"]["message_id"], self.context["uuid"])
        self.assertEqual(result["evidence"]["turn_key"], self.captured.turn_key)
        self.assertEqual(result["evidence"]["tool_id"], self.tool_id)

    def test_quoted_user_text_and_missing_tool_result_are_not_context_receipts(self):
        quote = self.node("user", self.tool["uuid"], message={"content": self.text})
        self.append(quote)
        self.assertEqual(self.verify()["status"], "unknown")
        self.context["parentUuid"] = self.tool["uuid"]
        self.append(self.context)
        self.assertEqual(self.verify()["status"], "unknown")

    def test_wrong_context_recipient_version_sidechain_tool_and_rendering_are_unknown(self):
        for field, value in (("sessionId", str(uuid.uuid4())), ("session_id", str(uuid.uuid4())),
                             ("version", "2.1.310"), ("isSidechain", True), ("renderedRole", "user"),
                             ("rendered", []), ("parentUuid", str(uuid.uuid4()))):
            with self.subTest(field=field):
                node = {**self.context, field: value}
                self.write(self.baseline)
                self.append(self.result, self.success, node)
                self.assertEqual(self.verify()["status"], "unknown")
        for field, value in (("toolUseID", "other"), ("hookEvent", "PreToolUse"),
                             ("content", [self.text, "extra"]), ("hookName", "PostToolUse:Other")):
            self.write(self.baseline)
            node = deepcopy(self.context)
            node["attachment"][field] = value
            self.append(self.result, self.success, node)
            self.assertEqual(self.verify()["status"], "unknown")

    def test_another_prompt_or_another_result_cannot_borrow_context(self):
        for mutation in ("prompt", "result", "error"):
            self.write(self.baseline)
            result = deepcopy(self.result)
            if mutation == "prompt":
                result["promptId"] = str(uuid.uuid4())
            elif mutation == "result":
                result["message"]["content"][0]["tool_use_id"] = "other"
            else:
                result["message"]["content"][0]["is_error"] = True
            self.append(result, self.success, self.context)
            self.assertEqual(self.verify()["status"], "unknown")

    def test_same_prompt_id_on_tool_results_does_not_replace_the_original_prompt(self):
        prior_tool = deepcopy(self.tool)
        prior_tool["uuid"] = str(uuid.uuid4())
        prior_tool["message"]["content"][0]["id"] = "prior-tool"
        prior_result = self.node("user", prior_tool["uuid"], promptId=self.prompt,
            message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "prior-tool"}]})
        self.tool["parentUuid"] = prior_result["uuid"]
        self.baseline = [self.user, prior_tool, prior_result, self.tool]
        self.write(self.baseline)
        self.request = receipts.prepare_context_receipt(self.captured)
        self.attempt["receipt_request"] = self.request
        self.assertEqual(self.request["prompt_uuid"], self.user["uuid"])
        self.result["promptId"] = self.prompt
        self.append(self.result, self.success, self.context)
        self.assertEqual(self.verify()["status"], "accepted")

    def test_baseline_replacement_is_unknown(self):
        self.append(self.result, self.success, self.context)
        content = self.path.read_bytes()
        temporary = self.path.with_suffix(".replacement")
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.assertEqual(self.verify()["status"], "unknown")

    def test_baseline_truncation_and_changed_prefix_are_unknown(self):
        original = self.path.read_bytes()
        for data in (b"{}\n", original.replace(b"Original work", b"Changed work")):
            self.path.write_bytes(data)
            self.assertEqual(self.verify()["status"], "unknown")

    def test_path_replacement_during_fd_snapshot_is_rejected(self):
        original = os.fstat
        calls = 0

        def replace_after_read(descriptor):
            nonlocal calls
            calls += 1
            facts = original(descriptor)
            if calls == 2:
                replacement = self.path.with_suffix(".new")
                replacement.write_bytes(self.path.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, self.path)
            return facts

        with patch.object(receipts.os, "fstat", side_effect=replace_after_read):
            with self.assertRaisesRegex(NativeError, "changed during inspection"):
                receipts._snapshot(self.path, receipts._deadline(.5))

    def test_large_flat_json_walk_checks_its_deadline_inside_the_line(self):
        line = (json.dumps({"flat": list(range(2048))}) + "\n").encode()
        with patch.object(receipts, "_check", side_effect=[None, NativeError("deadline")]) as check:
            with self.assertRaisesRegex(NativeError, "deadline"):
                receipts._records(line, 1)
        self.assertEqual(check.call_count, 2)

    def test_incomplete_append_is_unknown_until_native_record_finishes(self):
        self.append(self.result, self.success)
        encoded = json.dumps(self.context).encode()
        with self.path.open("ab") as stream:
            stream.write(encoded)
        self.assertEqual(self.verify()["status"], "unknown")
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.verify()["status"], "accepted")

    def test_duplicates_and_cycles_are_unknown(self):
        self.append(self.result, self.success, self.context, self.context)
        self.assertEqual(self.verify()["status"], "unknown")
        self.write(self.baseline)
        self.context["parentUuid"] = self.context["uuid"]
        self.append(self.context)
        self.assertEqual(self.verify()["status"], "unknown")
        self.write(self.baseline)
        with self.path.open("a") as stream:
            stream.write('{"type":"attachment","type":"user"}\n')
        self.assertEqual(self.verify()["status"], "unknown")

    def test_capture_requires_actual_owner_supported_profile_and_recorded_tool(self):
        with patch.object(receipts.claude, "current_identity", return_value=replace(self.identity, target={})):
            with self.assertRaises(NativeError):
                receipts.prepare_context_receipt(self.captured)
        for captured in (replace(self.captured, tool_id="absent"),
                         replace(self.captured, turn_key="claude:" + self.session + ":" + str(uuid.uuid4())),
                         replace(self.captured, event="submit")):
            with self.assertRaises(NativeError):
                receipts.prepare_context_receipt(captured)

    def test_owner_inspection_counts_toward_capture_deadline(self):
        with patch.object(receipts, "_invoking", return_value=receipts._profile()), \
                patch.object(receipts.time, "monotonic", side_effect=[0, 1]), \
                patch.object(receipts, "_snapshot") as snapshot:
            with self.assertRaisesRegex(NativeError, "deadline"):
                receipts.prepare_context_receipt(self.captured)
        snapshot.assert_not_called()

    def test_owner_inspection_receives_only_the_remaining_capture_budget(self):
        owner = receipts.claude.current_identity
        owner.reset_mock()
        with patch.object(receipts.time, "monotonic", return_value=1):
            receipts._invoking(self.identity, 1.25)
        owner.assert_called_once_with(timeout=.25)

    def test_adapter_profile_changes_never_implicitly_certify_busy_context(self):
        for profile in (("2.1.311", "0" * 64), ("2.1.312", receipts._SHA256)):
            with self.subTest(profile=profile), \
                    patch.object(receipts.claude, "_cli_profile", return_value=profile):
                with self.assertRaisesRegex(NativeError, "not a certified context profile"):
                    receipts.prepare_context_receipt(self.captured)

    def test_private_canonical_path_and_modes_remain_required(self):
        self.path.chmod(0o640)
        with self.assertRaises(NativeError):
            receipts.prepare_context_receipt(self.captured)
        self.path.chmod(0o600)
        other = self.path.with_name("other.jsonl")
        other.symlink_to(self.path)
        with self.assertRaises(NativeError):
            receipts.prepare_context_receipt(replace(self.captured, transcript_path=str(other)))

    def test_request_owner_turn_tool_and_token_cannot_be_rebound(self):
        self.append(self.result, self.success, self.context)
        for field, value in (("actor", "claude:" + str(uuid.uuid4())), ("token", "another"),
                             ("expected_turn_key", "other"), ("tool_id", "other")):
            with patch.dict(self.attempt, {field: value}):
                self.assertEqual(self.verify()["status"], "unknown")

    def test_history_inspection_is_scoped_to_actual_invoker_and_known_format(self):
        self.assertEqual(receipts.inspect_context_history(self.identity)["status"], "unverified")
        self.assertEqual(receipts.inspect_context_history(self.identity, str(self.path))["status"], "supported")
        with patch.object(receipts.claude, "current_identity", return_value=replace(self.identity, target={})):
            self.assertEqual(receipts.inspect_context_history(self.identity, str(self.path))["status"], "unverified")
        with patch.object(receipts, "_invoking") as invoking:
            for timeout in (False, 0, -1, float("inf"), float("nan")):
                self.assertEqual(receipts.inspect_context_history(self.identity, timeout=timeout)["status"], "unverified")
            invoking.assert_not_called()

    def test_provider_dispatch_preserves_the_codex_verifier(self):
        for captured in (None, {}, replace(self.captured, identity=None)):
            with self.assertRaises(NativeError):
                native.prepare_context_receipt(captured)
        self.assertEqual(native.verify_context_receipt(None, {})["status"], "unknown")
        with patch("murmur.native.context_receipts.verify_context_receipt", return_value={"status": "unknown"}) as verify:
            identity = NativeIdentity("codex", self.session)
            native.verify_context_receipt(identity, {}, timeout=.2)
            verify.assert_called_once_with(identity, {}, timeout=.2)
        self.append(self.result, self.success, self.context)
        self.assertEqual(native.verify_context_receipt(self.identity, self.attempt)["status"], "accepted")



class ClaudeRecordBoundsTests(unittest.TestCase):
    def test_wide_valid_record_does_not_allocate_one_validation_frame_per_child(self):
        # This fits the existing line bound; width must not amplify validation
        # workspace into tens of megabytes of per-child tuples.
        line = b'{"type":"metadata","data":[' + b'0,' * 899_999 + b'0]}\n'
        tracemalloc.start()
        try:
            rows, index = receipts._records(line, time.monotonic() + 20)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(rows[0][1]["data"]), 900_000)
        self.assertEqual(index, {})
        self.assertLess(peak, 32 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
