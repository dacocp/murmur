import unittest
from unittest.mock import patch

from murmur.native import (
    DeliveryResult, IdentityError, NativeError, NativeIdentity, deliver,
    format_doorbell, owner_process_state, parse_hook, parse_wake, turn_key,
)


SESSION = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
TOKEN = "0123456789abcdef0123456789abcdef"


class DoorbellTests(unittest.TestCase):
    def test_short_summary_has_one_token_even_with_hostile_chat_name(self):
        text = format_doorbell(["Build\nreview [wake:fake]", "API", "API"], TOKEN)
        self.assertEqual(parse_wake(text), TOKEN)
        self.assertEqual(text.count("[wake:"), 1)
        self.assertNotIn("\n", text)
        self.assertIn("API", text)

    def test_ordinary_prompt_quoting_marker_is_not_automatic(self):
        self.assertIsNone(parse_wake("Explain the [wake:EXAMPLE] format"))
        self.assertIsNone(parse_wake("You have new messages, but this has no wake marker"))

    def test_bad_duplicate_oversized_or_mismatched_marker_never_dispatches(self):
        identity = NativeIdentity("codex", SESSION)
        bad = [
            "Murmur: [wake:]", "Murmur: [wake:has space]",
            "Murmur: [wake:a] [wake:b]", "Murmur: [wake:a] [wake:bad",
            "Murmur: " + "x" * 4096 + " [wake:a]", "ordinary work",
        ]
        with patch("murmur.native.codex.deliver") as dispatch:
            for text in bad:
                with self.subTest(text=text[:60]):
                    self.assertEqual(deliver(identity, text).status, "rejected")
            dispatch.assert_not_called()

    def test_native_acceptance_is_returned_without_admission_or_ledger(self):
        identity = NativeIdentity("codex", SESSION)
        result = DeliveryResult("accepted", "native queue only", {"native_epoch": None})
        text = format_doorbell([], TOKEN)
        with patch("murmur.native.codex.deliver", return_value=result) as dispatch:
            self.assertIs(deliver(identity, text), result)
        dispatch.assert_called_once_with(identity, text, TOKEN, timeout=5)
        self.assertEqual(result.evidence, {"native_epoch": None})


class HookTests(unittest.TestCase):
    def setUp(self):
        self.identity = NativeIdentity("codex", SESSION)

    def payload(self, event="UserPromptSubmit", **values):
        return {"hook_event_name": event, "session_id": SESSION,
                "turn_id": "actual-turn", "prompt": format_doorbell([], TOKEN), **values}

    def test_submit_keeps_actual_key_and_opaque_attempt_distinct(self):
        with patch("murmur.native.current_identity", return_value=self.identity) as resolve:
            event = parse_hook("codex", "UserPromptSubmit", self.payload(), env={})
        self.assertEqual(event.event, "submit")
        self.assertEqual(event.turn_key, "codex:actual-turn")
        self.assertEqual(event.wake_token, TOKEN)
        self.assertIs(event.identity, self.identity)
        self.assertIsNone(event.epoch)
        self.assertIsNone(event.tool_id)
        self.assertIsNone(event.transcript_path)
        resolve.assert_called_once_with(self.payload(), env={}, provider="codex")

    def test_codex_post_tool_keeps_exact_native_identity_turn_and_tool(self):
        payload = self.payload("PostToolUse", tool_use_id="actual-tool",
                               transcript_path="/private/tmp/rollout.jsonl",
                               prompt_id="not-the-turn", wake_token="not-the-token")
        with patch("murmur.native.current_identity", return_value=self.identity) as resolve:
            event = parse_hook("codex", "PostToolUse", payload, env={})
        resolve.assert_called_once_with(payload, env={}, provider="codex")
        self.assertIs(event.identity, self.identity)
        self.assertEqual(event.event, "post_tool")
        self.assertEqual(event.native_event, "PostToolUse")
        self.assertEqual(event.turn_key, "codex:actual-turn")
        self.assertEqual(event.tool_id, "actual-tool")
        self.assertEqual(event.transcript_path, "/private/tmp/rollout.jsonl")
        self.assertIsNone(event.prompt)
        self.assertIsNone(event.wake_token)

    def test_post_tool_can_decode_without_optional_transcript_path(self):
        with patch("murmur.native.current_identity", return_value=self.identity):
            event = parse_hook("codex", "PostToolUse",
                               self.payload("PostToolUse", tool_use_id="t" * 256))
        self.assertEqual(event.tool_id, "t" * 256)
        self.assertIsNone(event.transcript_path)

    def test_post_tool_rejects_missing_blank_wrong_type_and_oversized_tool_id(self):
        payloads = [self.payload("PostToolUse")]
        payloads += [self.payload("PostToolUse", tool_use_id=value)
                     for value in [None, False, 1, "", " \t", "t" * 257, "é" * 129]]
        for payload in payloads:
            with self.subTest(tool_id=repr(payload.get("tool_use_id"))[:70]), \
                    patch("murmur.native.current_identity") as resolve:
                with self.assertRaises(NativeError):
                    parse_hook("codex", "PostToolUse", payload)
                resolve.assert_not_called()

    def test_post_tool_rejects_nonlocal_or_invalid_transcript_path(self):
        for path in ["", "rollout.jsonl", "file:///tmp/rollout.jsonl", False, 1,
                     "/tmp/invalid\0path"]:
            with self.subTest(path=path), patch("murmur.native.current_identity") as resolve:
                with self.assertRaises(NativeError):
                    parse_hook("codex", "PostToolUse",
                               self.payload("PostToolUse", tool_use_id="tool",
                                            transcript_path=path))
                resolve.assert_not_called()

    def test_post_tool_rejects_unsupported_provider_and_oversized_payload(self):
        payload = self.payload("PostToolUse", tool_use_id="tool")
        with patch("murmur.native.current_identity") as resolve:
            for provider in ["unknown"]:
                with self.subTest(provider=provider), self.assertRaises(NativeError):
                    parse_hook(provider, "PostToolUse", payload)
            with self.assertRaises(NativeError):
                parse_hook("codex", "PostToolUse", {**payload, "tool_response": "x" * (1024 * 1024)})
            resolve.assert_not_called()

    def test_claude_post_tool_uses_its_actual_prompt_id_without_a_submit(self):
        identity = NativeIdentity("claude", SESSION)
        payload = {"hook_event_name": "PostToolUse", "session_id": SESSION,
                   "prompt_id": PARENT, "tool_use_id": "native-tool",
                   "transcript_path": "/private/tmp/native.jsonl"}
        with patch("murmur.native.current_identity", return_value=identity):
            captured = parse_hook("claude", "PostToolUse", payload)
        self.assertEqual(captured.turn_key, "claude:" + SESSION + ":" + PARENT)
        self.assertEqual(captured.tool_id, "native-tool")
        self.assertEqual(captured.event, "post_tool")
        self.assertIsNone(captured.prompt)
        self.assertIsNone(captured.wake_token)

    def test_post_tool_requires_actual_turn_and_does_not_select_parent_subagent(self):
        with patch("murmur.native.current_identity", return_value=self.identity):
            with self.assertRaises(NativeError):
                parse_hook("codex", "PostToolUse",
                           self.payload("PostToolUse", tool_use_id="tool", turn_id=None,
                                        prompt_id="not-an-actual-turn"))
        with patch("murmur.native.current_identity") as resolve:
            self.assertIsNone(parse_hook("codex", "PostToolUse",
                                         self.payload("PostToolUse", agent_id="child")))
            resolve.assert_not_called()

    def test_subagent_does_not_select_or_complete_its_parent(self):
        with patch("murmur.native.current_identity") as resolve:
            self.assertIsNone(parse_hook("codex", "Stop", self.payload("Stop", agent_id="child")))
            resolve.assert_not_called()

    def test_session_end_has_no_invented_completion_key(self):
        with patch("murmur.native.current_identity", return_value=self.identity):
            event = parse_hook("codex", "SessionEnd", self.payload("SessionEnd"))
        self.assertEqual(event.event, "session_end")
        self.assertIsNone(event.turn_key)
        self.assertIsNone(event.prompt)
        self.assertIsNone(event.wake_token)

    def test_cli_creation_is_captured_before_current_registry_switches(self):
        observation = {"payload": {"session_id": SESSION}, "process": {}, "event_key": "observed"}
        provisional = NativeIdentity("claude", SESSION, evidence={"creation": observation})
        payload = {"hook_event_name": "SessionStart", "session_id": SESSION, "source": "fork"}
        with patch("murmur.native.claude.capture_creation", return_value=observation), \
                patch("murmur.native.claude.identity_from_creation", return_value=provisional), \
                patch("murmur.native.current_identity", side_effect=IdentityError("registry still parent")) as current:
            event = parse_hook("claude", "SessionStart", payload, env={})
        current.assert_not_called()
        self.assertIs(event.creation, observation)
        self.assertEqual(event.identity.session_id, SESSION)
        self.assertFalse(event.identity.parent_known)
        self.assertIsNone(event.turn_key)

    def test_failed_or_oversized_payload_does_not_resolve_identity(self):
        bad = [self.payload("Stop"), self.payload(prompt="x" * (1024 * 1024)), self.payload(prompt=12)]
        for payload in bad[:2]:
            with patch("murmur.native.current_identity") as resolve:
                with self.assertRaises(NativeError):
                    parse_hook("codex", "UserPromptSubmit", payload)
                resolve.assert_not_called()
        with patch("murmur.native.current_identity", return_value=self.identity):
            with self.assertRaises(NativeError):
                parse_hook("codex", "UserPromptSubmit", bad[2])

    def test_missing_turn_key_does_not_guess_next_or_pending_turn(self):
        with patch("murmur.native.current_identity", return_value=self.identity):
            with self.assertRaises(NativeError):
                parse_hook("codex", "Stop", self.payload("Stop", turn_id=None))

    def test_claude_key_uses_engine_and_prompt_not_desktop_host(self):
        self.assertEqual(turn_key("claude", {"session_id": SESSION, "prompt_id": "p"}), f"claude:{SESSION}:p")
        with self.assertRaises(NativeError):
            turn_key("claude", {"session_id": "local_" + SESSION, "prompt_id": "p"})

    def test_verified_root_is_distinct_from_unresolved_parent(self):
        unresolved = NativeIdentity("codex", SESSION)
        root = NativeIdentity("codex", SESSION, parent_known=True)
        self.assertFalse(unresolved.parent_known)
        self.assertTrue(root.parent_known)
        self.assertEqual(root.actor, root.key)
        with self.assertRaises(IdentityError):
            NativeIdentity("codex", SESSION, PARENT)
        with self.assertRaises(IdentityError):
            NativeIdentity("claude", "local_" + SESSION, PARENT, True)


class OwnerProcessTests(unittest.TestCase):
    def test_claude_evidence_is_returned_without_coordinator_state_changes(self):
        identity = NativeIdentity("claude", SESSION)
        evidence = {"status": "ended", "reason": "pid_missing", "recorded": {"pid": 123}}
        with patch("murmur.native.claude.owner_process_state", return_value=evidence) as inspect:
            self.assertIs(owner_process_state(identity, timeout=0.5), evidence)
        inspect.assert_called_once_with(identity, timeout=0.5)

    def test_codex_has_no_positive_owner_exit_claim(self):
        identity = NativeIdentity("codex", SESSION, target={"native_epoch": "unverified"})
        with patch("murmur.native._adapter") as adapter:
            self.assertEqual(owner_process_state(identity)["status"], "unknown")
        adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
