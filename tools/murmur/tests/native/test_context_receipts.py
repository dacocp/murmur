"""Native context receipts from synthetic logs; never inspect native processes."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from murmur import native
from murmur.native import context_receipts
from murmur.native.types import HookEvent, NativeError, NativeIdentity


SHA_0163 = "1afb28c01d7d49e892bd75f11076b10f1451e34e0ee308a95e5ab7f69f9c84fe"
SHA_0164 = "d8990e3ee7acfbc26f91265b12a1a83f0fee40d0e0f8f1f40a37d4ec21f499cf"
SHA_ALPHA11 = "a04752e4d46a4d89e53e6f61c59c5349a638c8ce172ffbf391fd482b4d902a4e"
SHA_LINUX_0164 = "b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306"
SHA_0165 = "d84a54b031085f01e3ad915ea7c72ebea4dcf69e90f0d1e0e4a637e8280991d2"
SHA_0166 = "58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea"
SHA_ALPHA6 = "508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44"


class ContextReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "sessions"
        self.directory.mkdir()
        self.session_id, self.turn_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.tool_id = "exec-" + str(uuid.uuid4())
        self.token = "context_" + uuid.uuid4().hex
        self.message = native.format_doorbell(["project discussion"], self.token)
        self.path = self.directory / f"rollout-{self.session_id}.jsonl"
        self.identity = NativeIdentity("codex", self.session_id, target={
            "binary": "/test/codex", "cwd": str(self.root), "transport": "queue", "cli_args": [],
            "env": {"HOME": str(self.root), "CODEX_HOME": str(self.root), "CODEX_SQLITE_HOME": None}})
        self.captured = HookEvent(self.identity, "post_tool", "PostToolUse",
                                  turn_key=f"codex:{self.turn_id}", tool_id=self.tool_id,
                                  transcript_path=str(self.path))
        self.profile = {"pid": 123, "uid": os.getuid(), "birth": "Mon Oct 26 04:47:22 2026",
                        "executable": "/test/codex", "sha256": SHA_0163,
                        "version": "0.163.0-alpha.10.2"}
        self.owner = patch.object(context_receipts, "_owner_profile", side_effect=lambda deadline: deepcopy(self.profile))
        self.owner.start()
        self.addCleanup(self.owner.stop)
        self.reset_stream()

    def reset_stream(self, *, header=None, ordinal_base=0):
        meta = {"id": self.session_id, "session_id": self.session_id,
                "cli_version": self.profile["version"], "history_mode": "paginated"}
        if header:
            meta.update(header)
        self.path.write_bytes(self.encode({"type": "session_meta", "ordinal": ordinal_base, "payload": meta})
                              + self.encode({"type": "event_msg", "ordinal": ordinal_base + 1, "payload": {
                                  "type": "task_started", "turn_id": self.turn_id}}))
        self.path.chmod(0o600)

    @staticmethod
    def encode(row):
        return json.dumps(row, separators=(",", ":")).encode() + b"\n"

    def context_row(self, *, ordinal=2, text=None):
        metadata = {"turn_id": self.turn_id, "content_item_kinds": ["hooks.additional_context"]}
        if self.profile["sha256"] != SHA_0163:
            metadata["content_item_metadata"] = [{"provenance": {"type": "harness", "hook": True}}]
        return {"type": "response_item", "ordinal": ordinal, "payload": {
            "type": "message", "id": "msg_" + str(uuid.uuid4()), "role": "developer",
            "content": [{"type": "input_text", "text": self.message if text is None else text}],
            "internal_chat_message_metadata_passthrough": metadata}}

    def append(self, row):
        with self.path.open("ab") as stream:
            stream.write(row if isinstance(row, bytes) else self.encode(row))

    def attempt(self):
        request = native.prepare_context_receipt(self.captured)
        json.dumps(request)  # The coordinator must be able to persist the capture.
        return {"actor": self.identity.key, "token": self.token,
                "expected_turn_key": self.captured.turn_key, "tool_id": self.tool_id,
                "receipt_request": request, "emission": {"context": self.message}}

    def verify(self, attempt, identity=None):
        return native.verify_context_receipt(identity or self.identity, attempt, timeout=0.5)

    def assert_unknown(self, result):
        self.assertEqual(result.get("status"), "unknown", result)
        self.assertTrue(isinstance(result.get("reason"), str) and result["reason"], result)
        self.assertFalse(result.get("evidence"), result)

    def compacted_row(self, *, ordinal=2, padding=5 * 1024 * 1024 // 2):
        """Observed compaction keys with entirely synthetic nested history."""
        return {"timestamp": "2026-10-27T19:21:00Z", "type": "compacted", "ordinal": ordinal,
                "payload": {"message": "x" * padding,
                    "replacement_history": [self.context_row(ordinal=ordinal)],
                    "replacement_history_metadata": [], "retained_context": None,
                    "window_number": 2, "first_window_id": "fixture", "previous_window_id": "fixture",
                    "window_id": "fixture", "compaction_response_id": "fixture",
                    "latest_token_usage_record": {}, "resume_metadata": {}}}

    def test_large_alpha11_compaction_before_capture_keeps_nonzero_ordinal_and_exact_byte_proof(self):
        self.profile.update(version="0.164.0-alpha.11.1", sha256=SHA_ALPHA11)
        self.reset_stream(ordinal_base=19066)
        compacted = self.encode(self.compacted_row(ordinal=19068))
        self.assertGreater(len(compacted), 2 * 1024 * 1024)
        self.append(compacted)
        baseline = self.path.read_bytes()
        attempt = self.attempt()
        request = attempt["receipt_request"]
        self.assertEqual((request["schema"], request["ordinal_base"], request["line_count"]), (1, 19066, 3))
        self.assertEqual(request["offset"], len(baseline))
        row = self.context_row(ordinal=19069)
        self.append(row)
        result = self.verify(attempt)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual((result["evidence"]["ordinal"], result["evidence"]["byte_start"],
                          result["evidence"]["byte_end"]), (19069, len(baseline), self.path.stat().st_size))
        self.assertEqual(result["evidence"]["message_id"], row["payload"]["id"])

    def test_old_capture_crosses_large_compaction_without_accepting_its_embedded_fake_receipt(self):
        # Create the original schema-1 lower bound under the old decoder budget.
        with patch.object(context_receipts, "_MAX_LINE", 2 * 1024 * 1024):
            attempt = self.attempt()
        attempt["receipt_request"].pop("ordinal_base")
        original = deepcopy(attempt)
        self.append(self.compacted_row())
        self.assert_unknown(self.verify(attempt))
        start = self.path.stat().st_size
        row = self.context_row(ordinal=3)
        self.append(row)
        result = self.verify(attempt)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(result["evidence"]["message_id"], row["payload"]["id"])
        self.assertEqual(result["evidence"]["byte_start"], start)
        self.assertEqual(result["evidence"]["token"], original["token"])
        self.assertEqual(attempt, original)

    def test_large_compactions_cannot_hide_malformed_json_duplicate_fields_nonfinite_depth_or_ordinals(self):
        ordinary = self.encode(self.compacted_row())
        deep = self.compacted_row()
        nested = "leaf"
        for unused in range(130):
            nested = [nested]
        deep["payload"]["retained_context"] = nested
        variants = [ordinary[:-2] + b"\n",
                    ordinary.replace(b'"ordinal":2,', b'"ordinal":2,"ordinal":2,', 1),
                    ordinary.replace(b'"window_number":2,', b'"window_number":NaN,', 1),
                    ordinary.replace(b'"window_number":2,', b'"window_number":2,"window_number":3,', 1),
                    ordinary.replace(b'"ordinal":2,', b'"ordinal":99,', 1), self.encode(deep)]
        for phase in ("capture", "confirm"):
            for number, row in enumerate(variants):
                self.reset_stream()
                attempt = self.attempt() if phase == "confirm" else None
                self.append(row)
                if phase == "confirm":
                    self.append(self.context_row(ordinal=3))
                with self.subTest(phase=phase, variant=number):
                    if phase == "capture":
                        with self.assertRaises(NativeError):
                            native.prepare_context_receipt(self.captured)
                    else:
                        self.assert_unknown(self.verify(attempt))

    def test_partial_large_compaction_remains_unknown_until_complete_and_a_real_receipt_follows(self):
        attempt = self.attempt()
        row = self.encode(self.compacted_row())
        self.append(row[:-1])
        self.assert_unknown(self.verify(attempt))
        with self.assertRaisesRegex(NativeError, "incomplete"):
            native.prepare_context_receipt(self.captured)
        self.append(b"\n")
        self.assert_unknown(self.verify(attempt))
        self.append(self.context_row(ordinal=3))
        self.assertEqual(self.verify(attempt)["status"], "accepted")

    def test_eight_mib_record_boundary_is_independent_of_256_mib_snapshot_limit(self):
        self.assertEqual(context_receipts._MAX_LINE, 8 * 1024 * 1024)
        self.assertEqual(context_receipts._MAX_FILE, 256 * 1024 * 1024)
        overhead = len(self.encode(self.compacted_row(padding=0))) - 1
        row = self.encode(self.compacted_row(padding=8 * 1024 * 1024 - overhead))
        self.assertEqual(len(row) - 1, 8 * 1024 * 1024)  # JSONL delimiter is not JSON content.
        attempt = self.attempt()
        self.append(row)
        self.append(self.context_row(ordinal=3))
        self.assertEqual(self.verify(attempt)["status"], "accepted")
        self.assertEqual(self.attempt()["receipt_request"]["line_count"], 4)
        self.reset_stream()
        attempt = self.attempt()
        self.append(row[:-1] + b" \n")  # One extra valid JSON whitespace byte exceeds the decoder budget.
        with self.assertRaisesRegex(NativeError, "line exceeds"):
            native.prepare_context_receipt(self.captured)
        self.assertIn("line exceeds", self.verify(attempt)["reason"])
        self.reset_stream()
        attempt = self.attempt()
        with self.path.open("r+b") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(NativeError, "size is unsupported"):
            native.prepare_context_receipt(self.captured)
        self.assertIn("size is unsupported", self.verify(attempt)["reason"])

    @unittest.skipUnless(os.environ.get("MURMUR_LARGE_HISTORY_TEST") == "1",
                         "opt-in 136 MiB native-shaped context history")
    def test_alpha6_resumed_history_above_128_mib_captures_and_confirms_exact_appended_context(self):
        self.profile.update(version="0.166.0-alpha.6", sha256=SHA_ALPHA6)
        self.reset_stream(header={"cli_version": "0.164.0-alpha.11.1"}, ordinal_base=19066)
        for ordinal in range(19068, 19102):
            self.append(self.compacted_row(ordinal=ordinal, padding=4 * 1024 * 1024))
        with self.path.open("ab") as stream:
            for ordinal in range(19102, 44102):
                stream.write(self.encode({"type": "event_msg", "ordinal": ordinal,
                                          "payload": {"type": "token_count", "info": None}}))
        baseline_size = self.path.stat().st_size
        with self.path.open("rb") as stream:
            baseline_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        self.assertGreater(baseline_size, 128 * 1024 * 1024)
        attempt = self.attempt()
        request = attempt["receipt_request"]
        self.assertEqual((request["offset"], request["ordinal_base"], request["line_count"]),
                         (baseline_size, 19066, 25036))
        self.assertEqual(request["prefix_sha256"], baseline_hash)
        self.assertEqual(request["profile"], self.profile)
        self.assert_unknown(self.verify(attempt))  # Context nested in compactions is never admission.
        row = self.context_row(ordinal=44102)
        self.append(row)
        result = self.verify(attempt)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual((result["evidence"]["ordinal"], result["evidence"]["byte_start"],
                          result["evidence"]["byte_end"], result["evidence"]["message_id"]),
                         (44102, baseline_size, self.path.stat().st_size, row["payload"]["id"]))

    def test_0165_resumed_history_above_32_mib_captures_and_confirms_exact_appended_context(self):
        self.profile.update(version="0.165.0-alpha.1.2", sha256=SHA_0165)
        self.reset_stream(header={"cli_version": "0.164.0-alpha.11.1"}, ordinal_base=19066)
        self.path.chmod(0o644)  # Native histories may be readable, never writable, by other users.
        for ordinal in range(19068, 19077):
            self.append(self.compacted_row(ordinal=ordinal, padding=4 * 1024 * 1024))
        baseline_size = self.path.stat().st_size
        self.assertGreater(baseline_size, 32 * 1024 * 1024)
        attempt = self.attempt()
        request = attempt["receipt_request"]
        self.assertEqual((request["offset"], request["ordinal_base"], request["line_count"]),
                         (baseline_size, 19066, 11))
        self.assertEqual(request["profile"], self.profile)
        self.assert_unknown(self.verify(attempt))  # Nested replacement history is never a new receipt.
        row = self.context_row(ordinal=19077)
        self.append(row)
        result = self.verify(attempt)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual((result["evidence"]["ordinal"], result["evidence"]["byte_start"],
                          result["evidence"]["byte_end"], result["evidence"]["message_id"]),
                         (19077, baseline_size, self.path.stat().st_size, row["payload"]["id"]))

    def test_late_json_decode_cannot_capture_or_confirm_even_with_fewer_than_1024_nodes(self):
        original = context_receipts.json.loads
        for phase in ("capture", "confirm"):
            self.reset_stream()
            attempt = self.attempt() if phase == "confirm" else None
            self.append(self.compacted_row() if phase == "capture" else self.context_row())
            now = [0]
            def late_decode(data, **options):
                value = original(data, **options)
                if value.get("type") == ("compacted" if phase == "capture" else "response_item"):
                    now[0] = 1
                return value
            with self.subTest(phase=phase), \
                    patch.object(context_receipts.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(context_receipts.json, "loads", side_effect=late_decode):
                if phase == "capture":
                    with self.assertRaisesRegex(NativeError, "deadline"):
                        native.prepare_context_receipt(self.captured)
                else:
                    self.assertIn("deadline", self.verify(attempt)["reason"])

    def test_capture_hashing_and_final_receipt_provenance_must_finish_before_the_deadline(self):
        now, original = [0], context_receipts.hashlib.sha256
        size = self.path.stat().st_size
        def late_hash(data):
            result = original(data)
            if len(data) == size:
                now[0] = 1
            return result
        with patch.object(context_receipts.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(context_receipts.hashlib, "sha256", side_effect=late_hash):
            with self.assertRaisesRegex(NativeError, "deadline"):
                native.prepare_context_receipt(self.captured)
        self.profile.update(version="0.164.0-alpha.11.1", sha256=SHA_ALPHA11)
        self.reset_stream()
        attempt = self.attempt()
        self.append(self.context_row())
        now[0] = 0
        provenance = context_receipts._hook_provenance
        def late_provenance(value):
            result = provenance(value)
            now[0] = 1
            return result
        with patch.object(context_receipts.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(context_receipts, "_hook_provenance", side_effect=late_provenance):
            self.assertIn("deadline", self.verify(attempt)["reason"])

    def test_exact_context_receipt_is_repeatable_and_verification_writes_nothing(self):
        attempt = self.attempt()
        row = self.context_row()
        self.append(row)
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns, sorted(self.root.rglob("*"))
        first, second = self.verify(attempt), self.verify(attempt)
        self.assertEqual(first.get("status"), "accepted", first)
        self.assertTrue(first.get("evidence"), first)
        evidence = first["evidence"]
        self.assertEqual({key: evidence[key] for key in (
            "actor", "session_id", "turn_key", "tool_id", "token", "message_id", "ordinal", "source")},
            {"actor": self.identity.key, "session_id": self.session_id,
             "turn_key": self.captured.turn_key, "tool_id": self.tool_id, "token": self.token,
             "message_id": row["payload"]["id"], "ordinal": 2, "source": "codex-canonical-jsonl"})
        self.assertEqual(evidence["byte_start"], attempt["receipt_request"]["offset"])
        self.assertEqual(evidence["byte_end"], self.path.stat().st_size)
        self.assertEqual(evidence["profile"], self.profile)
        self.assertEqual(first, second)
        legacy = deepcopy(attempt)
        legacy["receipt_request"].pop("ordinal_base")
        self.assertEqual(self.verify(legacy), first)
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns,
                                  sorted(self.root.rglob("*"))))
        self.assertEqual(attempt["emission"]["context"], self.message)

    def test_modern_profiles_require_harness_hook_provenance(self):
        for sha, version, creation in ((SHA_0164, "0.164.0", "0.164.0"),
                                       (SHA_LINUX_0164, "0.164.0", "0.164.0"),
                                       (SHA_0165, "0.165.0-alpha.1.2", "0.165.0-alpha.1.2"),
                                       (SHA_0165, "0.165.0-alpha.1.2", "0.164.0-alpha.11.1"),
                                       (SHA_0166, "0.166.0-alpha.5.1", "0.166.0-alpha.5.1"),
                                       (SHA_0166, "0.166.0-alpha.5.1", "0.164.0-alpha.11.1"),
                                       (SHA_ALPHA6, "0.166.0-alpha.6", "0.166.0-alpha.6"),
                                       (SHA_ALPHA6, "0.166.0-alpha.6", "0.164.0-alpha.11.1"),
                                       (SHA_ALPHA6, "0.166.0-alpha.6", "0.162.0-alpha.2")):
            self.profile.update(version=version, sha256=sha)
            for provenance in ("valid", None, {"type": "harness", "hook": False},
                               {"type": "harness", "hook": 1},
                               {"type": "client", "hook": True}):
                self.reset_stream(header={"cli_version": creation})
                attempt = self.attempt()
                row = self.context_row()
                if provenance is None:
                    row["payload"]["internal_chat_message_metadata_passthrough"].pop("content_item_metadata")
                elif provenance != "valid":
                    row["payload"]["internal_chat_message_metadata_passthrough"]["content_item_metadata"] = [
                        {"provenance": provenance}]
                self.append(row)
                with self.subTest(version=version, creation=creation, provenance=provenance):
                    result = self.verify(attempt)
                    if provenance == "valid":
                        self.assertEqual(result.get("status"), "accepted", result)
                    else:
                        self.assert_unknown(result)

    def test_alpha11_observed_record_shape_requires_exact_harness_hook_provenance(self):
        self.profile.update(version="0.164.0-alpha.11.1", sha256=SHA_ALPHA11)
        # Measured native shape; every identifier, timestamp and body is synthetic.
        fixture = Path(__file__).parent / "fixtures" / "codex-alpha11-hook-context.json"
        for provenance in ("valid", None, {"type": "harness", "hook": False},
                           {"type": "harness", "hook": 1}, {"type": "user", "hook": True}):
            self.reset_stream()
            attempt = self.attempt()
            row = json.loads(fixture.read_text())
            row["payload"]["content"][0]["text"] = self.message
            metadata = row["payload"]["internal_chat_message_metadata_passthrough"]
            metadata["turn_id"] = self.turn_id
            if provenance is None:
                metadata.pop("content_item_metadata")
            elif provenance != "valid":
                metadata["content_item_metadata"] = [{"provenance": provenance}]
            self.append(row)
            with self.subTest(provenance=provenance):
                result = self.verify(attempt)
                if provenance == "valid":
                    self.assertEqual(result["status"], "accepted", result)
                    self.assertEqual(result["evidence"]["profile"], self.profile)
                else:
                    self.assert_unknown(result)

    def test_alpha11_never_accepts_an_unmeasured_owner_or_resumed_older_header(self):
        for sha, version, header in (("f" * 64, "0.164.0-alpha.11.1", "0.164.0-alpha.11.1"),
                                     (SHA_LINUX_0164, "0.164.0", "0.163.0-alpha.10.2"),
                                     (SHA_LINUX_0164, "0.163.0-alpha.10.2", "0.163.0-alpha.10.2"),
                                     (SHA_ALPHA11, "0.164.0", "0.164.0"),
                                     (SHA_ALPHA11, "0.164.0-alpha.11.1", "0.163.0-alpha.10.2")):
            self.profile.update(sha256=sha, version=version)
            self.reset_stream(header={"cli_version": header})
            with self.subTest(sha=sha, version=version, header=header), self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)

    def test_0165_creation_header_compatibility_is_scoped_to_the_exact_append_owner(self):
        for sha, version, creation, supported in (
                (SHA_0165, "0.165.0-alpha.1.2", "0.165.0-alpha.1.2", True),
                (SHA_0165, "0.165.0-alpha.1.2", "0.164.0-alpha.11.1", True),
                (SHA_0165, "0.165.0-alpha.1.2", "0.163.0-alpha.10.2", False),
                (SHA_0165, "0.165.0-alpha.1.2", "0.164.0", False),
                (SHA_0165, "0.165.0-alpha.1.2", "unmeasured-creation", False),
                (SHA_0165, "0.164.0-alpha.11.1", "0.164.0-alpha.11.1", False),
                (SHA_ALPHA11, "0.164.0-alpha.11.1", "0.165.0-alpha.1.2", False),
                ("f" * 64, "0.165.0-alpha.1.2", "0.164.0-alpha.11.1", False)):
            self.profile.update(sha256=sha, version=version)
            self.reset_stream(header={"cli_version": creation})
            with self.subTest(sha=sha, version=version, creation=creation):
                if not supported:
                    with self.assertRaises(NativeError):
                        self.attempt()
                    continue
                attempt = self.attempt()
                self.append(self.context_row())
                self.assertEqual(self.verify(attempt)["status"], "accepted")

    def test_0165_resumed_receipt_keeps_frozen_owner_and_exact_binding_after_owner_exit(self):
        self.profile.update(sha256=SHA_0165, version="0.165.0-alpha.1.2")
        self.reset_stream(header={"cli_version": "0.164.0-alpha.11.1"}, ordinal_base=4871)
        attempt = self.attempt()
        self.append(self.context_row(ordinal=4873))
        with patch.object(context_receipts, "_owner_profile", side_effect=NativeError("owner exited")):
            result = self.verify(attempt)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(result["evidence"]["profile"], self.profile)
        for field, value in (("actor", "codex:" + str(uuid.uuid4())),
                             ("expected_turn_key", "codex:" + str(uuid.uuid4())),
                             ("tool_id", "another-tool"), ("token", "another_token")):
            changed = deepcopy(attempt)
            changed[field] = value
            with self.subTest(field=field):
                self.assert_unknown(self.verify(changed))
        self.append(b'{"type":"event_msg"')
        self.assertEqual(self.verify(attempt), result)

    def test_older_append_owners_keep_their_creation_pair_and_exact_profile_guards(self):
        for sha, version, creation, supported in (
                (SHA_0166, "0.166.0-alpha.5.1", "0.166.0-alpha.5.1", True),
                (SHA_0166, "0.166.0-alpha.5.1", "0.164.0-alpha.11.1", True),
                (SHA_0166, "0.166.0-alpha.5.1", "0.165.0-alpha.1.2", False),
                (SHA_0166, "0.166.0-alpha.5.1", "0.163.0-alpha.10.2", False),
                (SHA_0166, "0.166.0-alpha.5.1", "unmeasured-creation", False),
                (SHA_0166, "0.165.0-alpha.1.2", "0.164.0-alpha.11.1", False),
                (SHA_ALPHA6, "0.166.0-alpha.5.1", "0.164.0-alpha.11.1", False),
                (SHA_0166, "0.166.0-alpha.5.1", "0.166.0-alpha.6", False),
                (SHA_0165, "0.165.0-alpha.1.2", "0.166.0-alpha.5.1", False),
                ("f" * 64, "0.166.0-alpha.5.1", "0.164.0-alpha.11.1", False)):
            self.profile.update(sha256=sha, version=version)
            self.reset_stream(header={"cli_version": creation}, ordinal_base=19066)
            with self.subTest(sha=sha, version=version, creation=creation):
                if not supported:
                    with self.assertRaises(NativeError):
                        self.attempt()
                    continue
                attempt = self.attempt()
                row = self.context_row(ordinal=19068)
                self.append(row)
                result = self.verify(attempt)
                self.assertEqual(result["status"], "accepted", result)
                self.assertEqual(result["evidence"]["profile"], self.profile)
                self.assertEqual(result["evidence"]["message_id"], row["payload"]["id"])
                self.assertEqual(result["evidence"]["byte_start"], attempt["receipt_request"]["offset"])

    def test_alpha6_historical_creator_is_diagnostic_and_current_writer_proves_appended_receipt(self):
        self.profile.update(version="0.166.0-alpha.6", sha256=SHA_ALPHA6)
        # The reported old header had ordinal 8921. Identifiers and all content
        # here are synthetic; no native session history is copied or rewritten.
        for creator in ("0.162.0-alpha.2", "0.164.0", "0.164.0-alpha.11.1",
                        "0.165.0-alpha.1.2", "0.166.0-alpha.5.1", "0.166.0-alpha.6",
                        "unknown historical creator", "x" * 128, "é" * 128):
            with self.subTest(creator=creator):
                self.reset_stream(header={"cli_version": creator}, ordinal_base=8921)
                baseline = self.path.read_bytes()
                attempt = self.attempt()
                request = attempt["receipt_request"]
                self.assertEqual(request["ordinal_base"], 8921)
                self.assertEqual(request["profile"], self.profile)
                self.assertEqual(request["prefix_sha256"], hashlib.sha256(baseline).hexdigest())
                self.assertEqual(self.path.read_bytes(), baseline)
                self.assert_unknown(self.verify(attempt))
                row = self.context_row(ordinal=8923)
                self.append(row)
                result = self.verify(attempt)
                self.assertEqual(result["status"], "accepted", result)
                evidence = result["evidence"]
                self.assertEqual((evidence["ordinal"], evidence["byte_start"], evidence["message_id"]),
                                 (8923, len(baseline), row["payload"]["id"]))
                self.assertEqual(evidence["profile"], self.profile)
                # A creator label neither authorizes a different writer nor
                # changes an already frozen receipt when its owner disappears.
                with patch.object(context_receipts, "_owner_profile", side_effect=NativeError("owner ended")):
                    self.assertEqual(self.verify(attempt), result)
                for field, value in (("expected_turn_key", "codex:" + str(uuid.uuid4())),
                                     ("token", "another-token")):
                    changed = deepcopy(attempt)
                    changed[field] = value
                    self.assert_unknown(self.verify(changed))

    def test_alpha6_creator_metadata_rejects_missing_malformed_or_unbounded_values(self):
        self.profile.update(version="0.166.0-alpha.6", sha256=SHA_ALPHA6)
        for creator in (None, "", " ", " old", "old ", "old\ncreator", "old\x00creator",
                        "old\x7fcreator", "old\x85creator", "old\ud800creator", "x" * 129,
                        True, 162, [], {}):
            with self.subTest(creator=repr(creator)):
                self.reset_stream(header={"cli_version": creator}, ordinal_base=8921)
                with self.assertRaises(NativeError):
                    self.attempt()
        self.reset_stream(ordinal_base=8921)
        rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        rows[0]["payload"].pop("cli_version")
        self.path.write_bytes(b"".join(self.encode(row) for row in rows))
        with self.assertRaises(NativeError):
            self.attempt()

    def test_alpha6_historical_headers_do_not_relax_recipient_prefix_or_current_profile(self):
        self.profile.update(version="0.166.0-alpha.6", sha256=SHA_ALPHA6)
        for change in ({"id": str(uuid.uuid4())}, {"session_id": str(uuid.uuid4())},
                       {"history_mode": "unrecognized"}):
            self.reset_stream(header={"cli_version": "0.162.0-alpha.2", **change}, ordinal_base=8921)
            with self.subTest(change=change), self.assertRaises(NativeError):
                self.attempt()
        for change in ("inode", "creator", "ordinal"):
            self.reset_stream(header={"cli_version": "0.162.0-alpha.2"}, ordinal_base=8921)
            attempt = self.attempt()
            if change == "inode":
                self.path.rename(self.path.with_suffix(".old"))
                self.path.write_bytes(self.path.with_suffix(".old").read_bytes())
            else:
                rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
                if change == "creator":
                    rows[0]["payload"]["cli_version"] = "0.162.0-alpha.3"
                else:
                    rows[0]["ordinal"] = 8920
                self.path.write_bytes(b"".join(self.encode(row) for row in rows))
                self.assertEqual(self.path.stat().st_size, attempt["receipt_request"]["offset"])
            self.path.chmod(0o600)
            self.append(self.context_row(ordinal=8923))
            with self.subTest(change=change):
                self.assert_unknown(self.verify(attempt))
        for sha, version in (("f" * 64, "0.166.0-alpha.6"),
                             (SHA_ALPHA6, "0.166.0-alpha.5.1"),
                             (SHA_0164, "0.164.0"), (SHA_0166, "0.166.0-alpha.5.1")):
            self.profile.update(sha256=sha, version=version)
            self.reset_stream(header={"cli_version": "0.162.0-alpha.2"}, ordinal_base=8921)
            with self.subTest(sha=sha, version=version), self.assertRaises(NativeError):
                self.attempt()

    def test_fork_ordinal_base_is_frozen_and_appended_context_keeps_its_native_ordinal(self):
        for version, sha in (("0.163.0-alpha.10.2", SHA_0163), ("0.164.0", SHA_0164),
                             ("0.164.0", SHA_LINUX_0164)):
            with self.subTest(version=version):
                self.profile.update(version=version, sha256=sha)
                self.reset_stream(ordinal_base=4871, header={"forked_from_id": str(uuid.uuid4())})
                attempt = self.attempt()
                self.assertEqual(attempt["receipt_request"]["ordinal_base"], 4871)
                self.assertEqual(attempt["receipt_request"]["line_count"], 2)
                self.append({"type": "event_msg", "ordinal": 4873, "payload": {}})
                start = self.path.stat().st_size
                row = self.context_row(ordinal=4874)
                self.append(row)
                result = self.verify(attempt)
                self.assertEqual(result.get("status"), "accepted", result)
                self.assertEqual(result["evidence"]["ordinal"], 4874)
                self.assertEqual(result["evidence"]["message_id"], row["payload"]["id"])
                self.assertEqual(result["evidence"]["byte_start"], start)
                self.assertEqual(result["evidence"]["byte_end"], self.path.stat().st_size)
                self.assertEqual(self.verify(attempt), result)
                missing_base = deepcopy(attempt)
                missing_base["receipt_request"].pop("ordinal_base")
                self.assert_unknown(self.verify(missing_base))

    def test_header_ordinal_base_must_be_present_and_a_nonnegative_integer(self):
        for change, next_ordinal in (({}, 4872), ({"ordinal": None}, 4872),
                                     ({"ordinal": True}, 2), ({"ordinal": -1}, 0),
                                     ({"ordinal": "4871"}, 4872), ({"ordinal": 4871.0}, 4872)):
            self.reset_stream(ordinal_base=4871)
            rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
            rows[0].pop("ordinal")
            rows[0].update(change)
            rows[1]["ordinal"] = next_ordinal
            self.path.write_bytes(b"".join(self.encode(row) for row in rows))
            with self.subTest(change=change), self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)

    def test_frozen_ordinal_base_must_be_valid_and_match_the_header(self):
        self.reset_stream(ordinal_base=1)
        attempt = self.attempt()
        self.append(self.context_row(ordinal=3))
        self.assertEqual(self.verify(attempt).get("status"), "accepted")
        for change in ({}, {"ordinal_base": None}, {"ordinal_base": True}, {"ordinal_base": -1},
                       {"ordinal_base": "1"}, {"ordinal_base": 1.0}, {"ordinal_base": 2}):
            tampered = deepcopy(attempt)
            tampered["receipt_request"].pop("ordinal_base")
            tampered["receipt_request"].update(change)
            with self.subTest(change=change):
                self.assert_unknown(self.verify(tampered))

    def test_fork_prefix_and_appended_ordinals_must_remain_contiguous(self):
        for ordinal in (4870, 4871, 4873):
            self.reset_stream(ordinal_base=4871)
            rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
            rows[1]["ordinal"] = ordinal
            self.path.write_bytes(b"".join(self.encode(row) for row in rows))
            with self.subTest(phase="capture", ordinal=ordinal), self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)
        for ordinal in (4871, 4872, 4874):
            self.reset_stream(ordinal_base=4871)
            attempt = self.attempt()
            self.append({"type": "event_msg", "ordinal": ordinal, "payload": {}})
            self.append(self.context_row(ordinal=4874))
            with self.subTest(phase="verify", ordinal=ordinal):
                self.assert_unknown(self.verify(attempt))

    def test_matching_context_requires_its_exact_native_ordinal(self):
        for ordinal in (None, True, -1, 4872, 4874, 4873.0, "4873"):
            self.reset_stream(ordinal_base=4871)
            attempt = self.attempt()
            self.append(self.context_row(ordinal=ordinal))
            with self.subTest(ordinal=ordinal):
                self.assert_unknown(self.verify(attempt))

    def test_frozen_fork_header_cannot_be_rebased_or_reidentified(self):
        for change in ("base", "recipient"):
            self.reset_stream(ordinal_base=4871)
            attempt = self.attempt()
            rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
            if change == "base":
                rows[0]["ordinal"] = 4870
            else:
                rows[0]["payload"]["session_id"] = str(uuid.uuid4())
            self.path.write_bytes(b"".join(self.encode(row) for row in rows))
            self.append(self.context_row(ordinal=4873))
            with self.subTest(change=change):
                self.assert_unknown(self.verify(attempt))

    def test_matching_text_in_user_assistant_or_tool_output_is_not_a_receipt(self):
        for role in ("user", "assistant", "tool"):
            self.reset_stream()
            attempt = self.attempt()
            row = self.context_row()
            row["payload"]["role"] = role
            self.append(row)
            with self.subTest(role=role):
                self.assert_unknown(self.verify(attempt))
        self.reset_stream()
        attempt = self.attempt()
        self.append({"type": "response_item", "ordinal": 2, "payload": {
            "type": "function_call_output", "call_id": self.tool_id, "output": self.message}})
        self.assert_unknown(self.verify(attempt))

    def test_wrong_class_turn_body_or_content_shape_does_not_announce(self):
        variants = []
        for kinds in (None, [], ["user.text"], ["unknown"], ["hooks.additional_context", "user.text"]):
            row = self.context_row()
            row["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = kinds
            variants.append(row)
        row = self.context_row()
        row["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = str(uuid.uuid4())
        variants.append(row)
        row = self.context_row()
        row["payload"]["internal_chat_message_metadata_passthrough"].pop("turn_id")
        variants.append(row)
        for text in ('"' + self.message + '"', "Quoted context: " + self.message,
                     self.message + " extra text", self.message.replace(self.token, "other_token")):
            variants.append(self.context_row(text=text))
        row = self.context_row()
        row["payload"]["content"].append({"type": "input_text", "text": "unrelated"})
        variants.append(row)
        row = self.context_row()
        row["payload"]["content"][0]["type"] = "output_text"
        variants.append(row)
        for number, row in enumerate(variants):
            self.reset_stream()
            attempt = self.attempt()
            self.append(row)
            with self.subTest(number=number):
                self.assert_unknown(self.verify(attempt))

    def test_attempt_actor_turn_tool_and_token_must_match_the_capture(self):
        for field, wrong in (("actor", "codex:" + str(uuid.uuid4())),
                             ("expected_turn_key", "codex:" + str(uuid.uuid4())),
                             ("tool_id", "exec-other"), ("token", "other_token")):
            self.reset_stream()
            attempt = self.attempt()
            self.append(self.context_row())
            attempt[field] = wrong
            with self.subTest(field=field):
                self.assert_unknown(self.verify(attempt))

    def test_existing_context_before_capture_is_not_new_admission(self):
        self.append(self.context_row())
        attempt = self.attempt()
        self.assert_unknown(self.verify(attempt))

    def test_fork_cannot_reuse_its_parents_cursor_or_historical_context(self):
        attempt = self.attempt()
        self.append(self.context_row())
        child = replace(self.identity, session_id=str(uuid.uuid4()),
                        parent_session_id=self.session_id, parent_known=True)
        child_attempt = deepcopy(attempt)
        child_attempt["actor"] = child.key
        self.assert_unknown(self.verify(child_attempt, child))
        with self.assertRaises(NativeError):
            native.prepare_context_receipt(replace(self.captured, identity=child))

    def test_header_identity_version_and_mode_are_checked_before_capture(self):
        for change in ({"session_id": str(uuid.uuid4())}, {"id": str(uuid.uuid4())},
                       {"cli_version": "unproved-build"}, {"history_mode": "unrecognized"}):
            self.reset_stream(header=change)
            with self.subTest(change=change), self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)

    def test_replaced_truncated_or_changed_prefix_cannot_confirm(self):
        for change in ("replaced", "truncated", "prefix"):
            self.reset_stream()
            attempt = self.attempt()
            if change == "replaced":
                old = self.path.with_suffix(".old")
                self.path.rename(old)
                self.path.write_bytes(old.read_bytes())
            elif change == "truncated":
                self.path.write_bytes(b"\n")
            else:
                data = self.path.read_bytes().replace(b"task_started", b"task_changed")
                self.path.write_bytes(data)
            self.append(self.context_row())
            with self.subTest(change=change):
                self.assert_unknown(self.verify(attempt))

    def test_symlinks_and_other_writable_native_files_are_not_receipt_sources(self):
        self.path.chmod(0o666)
        with self.assertRaises(NativeError):
            native.prepare_context_receipt(self.captured)
        self.path.chmod(0o644)
        attempt = self.attempt()
        self.append(self.context_row())
        self.path.chmod(0o666)
        self.assert_unknown(self.verify(attempt))
        self.path.chmod(0o644)
        real = self.path.with_suffix(".real")
        self.path.rename(real)
        self.path.symlink_to(real)
        with self.assertRaises(NativeError):
            native.prepare_context_receipt(self.captured)
        self.assert_unknown(self.verify(attempt))

    def test_duplicate_keys_and_malformed_records_do_not_hide_before_valid_context(self):
        for bad in (b'{"type":"event_msg","ordinal":2,"ordinal":1,"payload":{}}\n',
                    b'{not-json}\n', b'[]\n',
                    b'{"type":"event_msg","ordinal":2,"payload":{"number":NaN}}\n',
                    b'{"type":"event_msg","ordinal":2,"payload":{"number":Infinity}}\n',
                    b'{"type":"event_msg","ordinal":99,"payload":{}}\n',
                    b'{"type":"event_msg","payload":{}}\n',
                    b'{"type":"event_msg","ordinal":true,"payload":{}}\n',
                    b'{"type":"event_msg","ordinal":2.0,"payload":{}}\n'):
            self.reset_stream()
            attempt = self.attempt()
            self.append(bad)
            self.append(self.context_row(ordinal=3))
            with self.subTest(bad=bad):
                self.assert_unknown(self.verify(attempt))

    def test_partial_native_context_remains_unknown_until_its_newline_arrives(self):
        attempt = self.attempt()
        data = self.encode(self.context_row())
        self.append(data[:-1])
        self.assert_unknown(self.verify(attempt))
        self.append(b"\n")
        result = self.verify(attempt)
        self.assertEqual(result.get("status"), "accepted", result)
        self.append(b'{"type":"event_msg"')
        self.assertEqual(self.verify(attempt), result)

    def test_missing_identity_or_native_post_tool_fields_do_not_capture(self):
        for change in ({"tool_id": None}, {"tool_id": ""}, {"transcript_path": None},
                       {"native_event": "UserPromptSubmit", "event": "submit"}, {"turn_key": None}):
            with self.subTest(change=change), self.assertRaises(NativeError):
                native.prepare_context_receipt(replace(self.captured, **change))

    def test_capture_certifies_owner_and_later_confirmation_uses_frozen_profile(self):
        attempt = self.attempt()
        self.append(self.context_row())
        with patch.object(context_receipts, "_owner_profile", side_effect=NativeError("unsupported native owner")):
            with self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)
            self.assertEqual(self.verify(attempt).get("status"), "accepted")
        original = deepcopy(self.profile)
        for change in ({"version": "uncertified-version"}, {"sha256": "0" * 64}):
            self.profile.update(original)
            self.profile.update(change)
            with self.subTest(change=change), self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)
            tampered = deepcopy(attempt)
            tampered["receipt_request"]["profile"].update(change)
            self.assert_unknown(self.verify(tampered))
        self.profile.update(original)
        self.profile["birth"] = "a replacement owner birth"
        self.assertEqual(self.verify(attempt).get("status"), "accepted")

    def test_capture_and_confirmation_enforce_distinct_file_line_and_depth_bounds(self):
        for phase in ("capture", "confirm"):
            for bound in ("file", "line", "depth"):
                self.reset_stream()
                attempt = self.attempt() if phase == "confirm" else None
                padding = "x" * 5000 if bound == "file" else "x" * 2048
                if bound == "depth":
                    padding = "leaf"
                    for unused in range(12):
                        padding = [padding]
                self.append({"type": "event_msg", "ordinal": 2, "payload": {"padding": padding}})
                if phase == "confirm":
                    self.append(self.context_row(ordinal=3))
                overrides = {"_MAX_FILE": 4096} if bound == "file" else {
                    "_MAX_LINE": 1024} if bound == "line" else {"_MAX_DEPTH": 8}
                with self.subTest(phase=phase, bound=bound), patch.multiple(context_receipts, **overrides):
                    if phase == "capture":
                        with self.assertRaises(NativeError):
                            native.prepare_context_receipt(self.captured)
                    else:
                        self.assert_unknown(self.verify(attempt))

    def test_capture_refuses_duplicate_header_keys_or_incomplete_baseline(self):
        data = self.path.read_bytes()
        encoded = json.dumps(self.session_id).encode()
        duplicate = data.replace(b'"session_id":' + encoded,
                                 b'"session_id":"wrong","session_id":' + encoded, 1)
        self.assertNotEqual(duplicate, data)
        self.path.write_bytes(duplicate)
        with self.assertRaises(NativeError):
            native.prepare_context_receipt(self.captured)
        self.reset_stream()
        self.append(b'{"type":"response_item"')
        with self.assertRaises(NativeError):
            native.prepare_context_receipt(self.captured)


if __name__ == "__main__":
    unittest.main()
