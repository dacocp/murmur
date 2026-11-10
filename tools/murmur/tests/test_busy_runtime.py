"""Busy hook/coordinator integration with real SQLite and a fake native boundary."""

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur import config, daemon, hooks
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger
from murmur.native import HookEvent, NativeError, NativeIdentity, parse_wake
from test_coordinator import FakeBackend


SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "codex:22222222-2222-4222-8222-222222222222"
NEXT = "codex:33333333-3333-4333-8333-333333333333"


class ReceiptTransport:
    def __init__(self):
        self.result = {"status": "unknown", "reason": "Native evidence not yet visible"}
        self.calls = []

    def verify_context_receipt(self, identity, attempt, *, timeout):
        self.calls.append((identity, attempt, timeout))
        return self.result


class BusyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.addCleanup(lambda: self.ledger.close())
        hooks.initialize(self.ledger.db)
        hooks.publish_control(self.ledger, True)
        self.identity = NativeIdentity("codex", SESSION, parent_known=True,
                                       target={"native_epoch": "fixture-owner"})
        self.actor = self.identity.actor
        self.binding = routing_binding(self.identity)
        self.ledger.enroll(self.actor, asdict(self.identity), "Murmur fixture", "self")
        self.ledger.sync_conversations(self.actor, [{"id": "build", "name": "Build review",
            "kind": "channel", "role": "member"}])
        self.arrival("request")
        self.transport = ReceiptTransport()
        self.coordinator = Coordinator(self.ledger, None, self.transport)

    def arrival(self, post_id):
        self.ledger.ingest(self.actor, [{"id": post_id, "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 101, "update_at": 101,
            "causal": "exchange"}], 1, 1)

    def post_tool(self, tool="tool-1", *, turn=TURN, identity=None, prepare_error=None):
        # Native parsing/provenance is separately verified by the adapter tests.
        captured = HookEvent(identity or self.identity, "post_tool", "PostToolUse", turn,
                             tool_id=tool, transcript_path="/fixture/native-stream")
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt",
                      return_value={"format": "native-fixture", "offset": 100},
                      side_effect=prepare_error):
            return hooks.handle(self.ledger, "codex", "PostToolUse", {})

    def finish(self, turn=TURN):
        captured = HookEvent(self.identity, "complete", "Stop", turn)
        with patch("murmur.native.parse_hook", return_value=captured):
            return hooks.handle(self.ledger, "codex", "Stop", {})

    def desktop_turn(self):
        bundle = "/fixture/ChatGPT.app"
        app = {"pid": 101, "parent_pid": 1, "uid": 501, "birth": "app birth",
               "executable": bundle + "/Contents/MacOS/ChatGPT", "argv_sha256": "a" * 64}
        owner = {**app, "pid": 201, "parent_pid": 101, "birth": "backend birth",
                 "executable": bundle + "/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"}
        desktop = {"schema": 1, "kind": "codex-desktop-deep-link-v1", "app_bundle": bundle,
                   "app": app, "owner": owner, "pipe": {"path": "/fixture/app.sock", "device": 1, "inode": 2},
                   "domain": {"home": "/fixture/user", "codex_home": "/fixture/user/.codex",
                              "sqlite_home": None, "user_data_path": None, "supported": True},
                   "startup_supported": True}
        self.identity = replace(self.identity, target={"binary": bundle + "/Contents/Resources/codex-cli/bin/codex",
            "cwd": "/fixture/native-workspace", "env": {"HOME": "/fixture/user", "CODEX_HOME": None,
                "CODEX_SQLITE_HOME": None}, "cli_args": [], "transport": "queue"},
            evidence={"desktop_wake": desktop})
        self.binding = routing_binding(self.identity)
        self.ledger.enroll(self.actor, asdict(self.identity), "Murmur fixture", "self")
        self.ledger.start_turn(self.actor, self.binding, TURN, native_identity=asdict(self.identity))
        return replace(self.identity, target={**self.identity.target, "cwd": "/fixture/tool-workspace"})

    def test_desktop_tool_cwd_uses_frozen_active_target_without_rebinding_or_duplicate(self):
        frozen = self.ledger.create_attempt(self.actor, self.binding, "old-pending",
            expected_identity=asdict(self.identity), refreshed_identity=asdict(self.identity))
        self.assertIsNotNone(frozen)
        tool = self.desktop_turn()
        self.arrival("cwd-request")
        active, enrolled, original_tool = self.ledger.active_turn(self.actor), self.ledger.actor(self.actor), asdict(tool)
        captured = HookEvent(tool, "post_tool", "PostToolUse", TURN,
                             tool_id="different-cwd", transcript_path="/fixture/native-stream")
        def prepare(event):
            self.assertEqual(event.identity.target, self.identity.target)
            self.assertEqual((event.turn_key, event.tool_id), (TURN, "different-cwd"))
            return {"format": "native-fixture", "offset": 100}
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", side_effect=prepare) as capture:
            result = hooks.handle(self.ledger, "codex", "PostToolUse", {})
        capture.assert_called_once()
        token = parse_wake(result["hookSpecificOutput"]["additionalContext"])
        attempt = self.ledger.attempt(token)
        self.assertEqual((attempt["native_identity"]["target"], attempt["post_ids"], attempt["status"]),
                         (self.identity.target, ["cwd-request"], "pending"))
        self.assertEqual(self.post_tool("different-cwd", identity=tool), {})
        self.assertEqual(len(self.ledger.attempts(self.actor)), 2)
        self.assertEqual(self.ledger.attempt("old-pending"), frozen)
        self.assertEqual(self.ledger.active_turn(self.actor), active)
        self.assertEqual({k: v for k, v in self.ledger.actor(self.actor).items() if k != "causal"},
                         {k: v for k, v in enrolled.items() if k != "causal"})
        self.assertEqual(asdict(tool), original_tool)
        self.assertIsNone(next(p for p in self.ledger.unread_posts(self.actor) if p["id"] == "cwd-request")["announced_at"])

    def test_tool_cwd_exception_rejects_other_routes_and_desktop_generations(self):
        tool = self.desktop_turn()
        variants = [replace(tool, evidence={"desktop_wake": None}),
                    replace(tool, parent_known=False, evidence={})]
        for key, value in (("binary", "/fixture/other-codex"), ("env", {**tool.target["env"], "CODEX_HOME": "/other"}),
                           ("cli_args", ["--profile", "other"]), ("transport", "other")):
            variants.append(replace(tool, target={**tool.target, key: value}))
        for key, value in (("app_bundle", "/other/ChatGPT.app"), ("schema", 2), ("startup_supported", False),
                           ("app", {**tool.evidence["desktop_wake"]["app"], "birth": "new app"}),
                           ("owner", {**tool.evidence["desktop_wake"]["owner"], "pid": 202}),
                           ("pipe", {"path": "/other/app.sock", "device": 1, "inode": 3}),
                           ("domain", {**tool.evidence["desktop_wake"]["domain"], "codex_home": "/other"})):
            variants.append(replace(tool, evidence={"desktop_wake": {**tool.evidence["desktop_wake"], key: value}}))
        missing_owner = deepcopy(tool.evidence["desktop_wake"])
        del missing_owner["owner"]
        variants.append(replace(tool, evidence={"desktop_wake": missing_owner}))
        active, enrolled = self.ledger.active_turn(self.actor), self.ledger.actor(self.actor)
        for variant in variants:
            with self.subTest(target=variant.target, evidence=variant.evidence):
                self.assertEqual(self.post_tool(identity=variant,
                    prepare_error=AssertionError("Invalid route reached receipt capture")), {})
                self.assertEqual(self.ledger.active_turn(self.actor), active)
                self.assertEqual(self.ledger.actor(self.actor), enrolled)
                self.assertEqual(self.ledger.attempts(self.actor), [])

    def test_tool_cwd_exception_requires_same_active_turn_binding_and_enrollment(self):
        tool = self.desktop_turn()
        for turn in (NEXT, "codex:stale-turn"):
            with self.subTest(turn=turn):
                self.assertEqual(self.post_tool(turn=turn, identity=tool,
                    prepare_error=AssertionError("Wrong turn reached capture")), {})
        self.ledger.db.execute("UPDATE native_turns SET epoch='different-binding' WHERE actor=?", (self.actor,))
        self.assertEqual(self.post_tool(identity=tool, prepare_error=AssertionError("Wrong binding reached capture")), {})
        self.ledger.db.execute("UPDATE native_turns SET epoch=? WHERE actor=?", (self.binding, self.actor))
        changed = replace(self.identity, target={**self.identity.target, "cwd": "/fixture/changed-enrollment"})
        self.ledger.enroll(self.actor, asdict(changed), "Murmur fixture", "self")
        self.assertEqual(self.post_tool(identity=tool, prepare_error=AssertionError("Changed enrollment reached capture")), {})
        self.ledger.enroll(self.actor, asdict(self.identity), "Murmur fixture", "self")
        self.ledger.interrupt(self.actor, TURN, self.binding)
        self.assertEqual(self.post_tool(identity=tool, prepare_error=AssertionError("No active turn reached capture")), {})
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertEqual(self.ledger.attempts(self.actor), [])

    def test_tool_cwd_exception_keeps_receipt_failures_quiet_and_new_submit_routes_explicit(self):
        tool = self.desktop_turn()
        active, enrolled = self.ledger.active_turn(self.actor), self.ledger.actor(self.actor)
        self.assertEqual(self.post_tool(identity=tool, prepare_error=NativeError("Unsupported native format")), {})
        self.assertEqual(self.ledger.active_turn(self.actor), active)
        self.assertEqual(self.ledger.actor(self.actor), enrolled)
        self.assertEqual(self.ledger.attempts(self.actor), [])
        submit = HookEvent(tool, "submit", "UserPromptSubmit", NEXT, prompt="Ordinary work")
        with patch("murmur.native.parse_hook", return_value=submit):
            self.assertEqual(hooks.handle(self.ledger, "codex", "UserPromptSubmit", {"prompt": "Ordinary work"}), {})
        self.assertEqual(self.ledger.actor(self.actor)["identity"]["target"], tool.target)
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], NEXT)
        self.assertEqual(self.ledger.active_turn(self.actor)["identity"]["target"], tool.target)

    def test_claude_context_uses_same_turn_and_requires_independent_native_receipt(self):
        identity = NativeIdentity("claude", SESSION, parent_known=True,
                                  target={"native_epoch": "claude-owner", "platform": "linux",
                                          "version": "2.1.311", "runtime": "cli"})
        actor, binding = identity.actor, routing_binding(identity)
        turn = "claude:" + SESSION + ":22222222-2222-4222-8222-222222222222"
        self.ledger.enroll(actor, asdict(identity), "Claude fixture", "claude-self")
        self.ledger.sync_conversations(actor, [{"id": "build", "name": "Build review",
                                               "kind": "channel", "role": "member"}])
        self.ledger.ingest(actor, [{"id": "claude-request", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 101, "update_at": 101,
            "causal": "exchange"}], 1, 1)
        self.ledger.start_turn(actor, binding, turn, native_identity=asdict(identity))
        original = self.ledger.active_turn(actor)
        captured = HookEvent(identity, "post_tool", "PostToolUse", turn, tool_id="claude-tool")
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", return_value={"native": "captured"}):
            output = hooks.handle(self.ledger, "claude", "PostToolUse", {})
        token = parse_wake(output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.ledger.attempt(token)["status"], "pending")
        self.assertEqual(self.ledger.active_turn(actor), original)
        with patch("murmur.native.parse_hook", return_value=replace(captured, tool_id="next-tool")), \
                patch("murmur.native.prepare_context_receipt", side_effect=AssertionError("Pending context holds capture")):
            self.assertEqual(hooks.handle(self.ledger, "claude", "PostToolUse", {}), {})
        self.transport.result = {"status": "accepted", "evidence": {"native_attachment": "exact"}}
        self.coordinator.reconcile_context(actor)
        self.assertEqual(self.ledger.attempt(token)["status"], "admitted")
        self.assertEqual(self.ledger.active_turn(actor), original)
        stop = HookEvent(identity, "complete", "Stop", turn)
        with patch("murmur.native.parse_hook", return_value=stop):
            hooks.handle(self.ledger, "claude", "Stop", {})
        self.assertEqual(self.ledger.attempt(token)["status"], "completed")
        self.assertIsNone(self.ledger.active_turn(actor))

    def test_valid_unsupported_mac_claude_busy_hook_is_quiet(self):
        identity = NativeIdentity("claude", SESSION, parent_known=True,
                                  target={"native_epoch": "mac-owner", "runtime": "cli", "version": "2.1.310"})
        actor, binding = identity.actor, routing_binding(identity)
        turn = "claude:" + SESSION + ":22222222-2222-4222-8222-222222222222"
        self.ledger.enroll(actor, asdict(identity), "Mac Claude", "mac-self")
        self.ledger.sync_conversations(actor, [{"id": "build", "name": "Build review",
                                               "kind": "channel", "role": "member"}])
        self.ledger.ingest(actor, [{"id": "mac-request", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 101, "update_at": 101,
            "causal": "exchange"}], 1, 1)
        self.ledger.start_turn(actor, binding, turn, native_identity=asdict(identity))
        original = self.ledger.active_turn(actor)
        before = self.ledger._unread(actor)
        captured = HookEvent(identity, "post_tool", "PostToolUse", turn, tool_id="mac-tool")
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", side_effect=AssertionError("Unsupported profile")):
            self.assertEqual(hooks.handle(self.ledger, "claude", "PostToolUse", {}), {})
        self.assertEqual(self.ledger.active_turn(actor), original)
        self.assertEqual(self.ledger._unread(actor), before)
        self.assertEqual(self.ledger.attempts(actor), [])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM diagnostics").fetchone()[0], 0)

    def test_context_only_handoff_preserves_task_until_independent_receipt(self):
        self.ledger.start_turn(self.actor, self.binding, TURN, native_identity=asdict(self.identity))
        original = self.ledger.active_turn(self.actor)
        result = self.post_tool()
        context = result["hookSpecificOutput"]["additionalContext"]
        token = parse_wake(context)
        self.assertEqual(result, {"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext":
            "You have new messages in ‘Build review’. [wake:" + token + "]"}})
        self.assertEqual(self.ledger.active_turn(self.actor), original)
        attempt = self.ledger.attempt(token)
        self.assertEqual(attempt["status"], "pending")
        self.assertEqual(attempt["emission"], {"context": context})
        self.assertEqual(attempt["receipt_request"], {"format": "native-fixture", "offset": 100})
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.assertEqual(self.post_tool("tool-2"), {})
        self.coordinator.reconcile_context(self.actor)
        self.assertEqual(self.ledger.attempt(token)["status"], "pending")
        self.transport.result = {"status": "accepted", "evidence": {"native_record": 123}}
        self.coordinator.reconcile_context(self.actor)
        self.assertEqual(self.ledger.attempt(token)["status"], "admitted")
        self.assertEqual(self.ledger.active_turn(self.actor), original)
        self.assertIsNotNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.assertEqual(self.post_tool("tool-3"), {})  # Ignored backlog is quiet.
        self.arrival("later")
        later = self.post_tool("tool-4")
        self.assertNotEqual(parse_wake(later["hookSpecificOutput"]["additionalContext"]), token)
        self.assertEqual(self.ledger.active_turn(self.actor), original)

    def test_midturn_registration_late_receipt_and_stale_tool_preserve_new_work(self):
        self.assertEqual(self.coordinator.activity(self.identity), {"state": "unknown"})
        unresolved = replace(self.identity, parent_known=False)
        result = self.post_tool(identity=unresolved)
        token = parse_wake(result["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.ledger.active_turn(self.actor)["identity"]["parent_known"])
        self.assertEqual(self.coordinator.activity(self.identity), {"state": "observed_busy", "turn_key": TURN})
        self.finish()
        self.assertEqual(self.coordinator.activity(self.identity), {"state": "observed_idle", "turn_key": TURN})
        self.ledger.start_turn(self.actor, self.binding, NEXT, native_identity=asdict(self.identity))
        current = self.ledger.active_turn(self.actor)
        self.transport.result = {"status": "accepted", "evidence": {"native_record": 123}}
        self.coordinator.reconcile_context(self.actor)
        self.assertEqual(self.ledger.attempt(token)["status"], "completed")
        self.assertEqual(self.ledger.active_turn(self.actor), current)
        self.assertEqual(self.post_tool("delayed-tool"), {})
        self.finish(TURN)
        self.assertEqual(self.ledger.active_turn(self.actor), current)

    def test_native_failure_never_blocks_or_replaces_tool_output(self):
        self.assertEqual(self.post_tool(prepare_error=NativeError("Unsupported native format")), {})
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], TURN)
        self.assertEqual(self.ledger.attempts(), [])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        error = self.ledger.db.execute("SELECT error FROM diagnostics ORDER BY id DESC").fetchone()[0]
        self.assertIn("Unsupported native format", error)

    def test_read_muted_and_unenrolled_sessions_do_not_emit(self):
        self.ledger.set_mute(self.actor, "build", True)
        self.assertEqual(self.post_tool(), {})
        self.ledger.set_mute(self.actor, "build", False)
        self.ledger.mark_read(self.actor, ["request"])
        self.assertEqual(self.post_tool("after-read"), {})
        other = replace(self.identity, session_id="44444444-4444-4444-8444-444444444444")
        self.assertEqual(self.post_tool("unenrolled", identity=other), {})
        self.assertEqual(len(self.ledger.actors()), 1)
        self.assertEqual(self.ledger.attempts(), [])

    def test_recipient_filter_survives_restart_and_leaves_other_enrolled_work_inert(self):
        other = replace(self.identity, session_id="44444444-4444-4444-8444-444444444444")
        self.ledger.enroll(other.actor, asdict(other), "Other enrolled work", "other-self")
        self.ledger.sync_conversations(other.actor, [{"id": "build", "name": "Build review",
            "kind": "channel", "role": "member"}])
        self.ledger.ingest(other.actor, [{"id": "other-request", "channel_id": "build", "user_id": "peer",
            "author": "Bea", "root_id": "", "create_at": 101, "update_at": 101}], 1, 1)
        hooks.publish_control(self.ledger, True, [self.actor])
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 100)
        captured = HookEvent(other, "post_tool", "PostToolUse", TURN,
                             tool_id="other-tool", transcript_path="/fixture/other-stream")
        with patch("murmur.native.parse_hook", return_value=captured), \
                patch("murmur.native.prepare_context_receipt", side_effect=AssertionError("Other actor is excluded")):
            self.assertEqual(hooks.handle(self.ledger, "codex", "PostToolUse", {}), {})
        self.assertIsNone(self.ledger.active_turn(other.actor))
        self.assertEqual(self.ledger.attempts(other.actor), [])
        self.assertIsNone(self.ledger.unread_posts(other.actor)[0]["announced_at"])
        self.assertIn("additionalContext", self.post_tool()["hookSpecificOutput"])
        hooks.publish_control(self.ledger, True, [])
        with patch("murmur.native.parse_hook", side_effect=AssertionError("Empty filter disables all")):
            self.assertEqual(hooks.handle(self.ledger, "codex", "PostToolUse", {}), {})

    def test_disabled_daemon_still_reconciles_receipts_and_new_arrivals(self):
        result = self.post_tool()
        token = parse_wake(result["hookSpecificOutput"]["additionalContext"])
        self.finish()
        config.save({"url": "http://fixture.invalid", "token": "fixture",
                     "delivery_enabled": False, "busy_context_enabled": True}, self.path.parent)
        backend = FakeBackend()
        backend.add("request")  # The existing cursor already covers this arrival.
        backend.add("later", when=102)
        receipt = {"status": "unknown", "reason": "Canonical record not yet visible"}
        with patch("murmur.control.Backend", return_value=backend), \
                patch("murmur.native.verify_context_receipt", side_effect=lambda *_a, **_k: receipt) as verify, \
                patch("murmur.native.deliver", side_effect=AssertionError("Delivery is disabled")):
            daemon.run(self.path.parent, once=True)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["request", "later"])
            self.assertEqual(self.ledger.attempt(token)["status"], "pending")
            error = self.ledger.db.execute("SELECT surface,error FROM diagnostics ORDER BY id DESC").fetchone()
            self.assertEqual(error["surface"], "native_receipt")
            self.assertIn("Canonical record not yet visible", error["error"])
            receipt = {"status": "accepted", "evidence": {"native_record": 123}}
            daemon.run(self.path.parent, once=True)
        self.assertEqual(self.ledger.attempt(token)["status"], "completed")
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertFalse(hooks.busy_context_enabled(self.ledger))
        unread = self.ledger.unread_posts(self.actor)
        self.assertIsNotNone(unread[0]["announced_at"])
        self.assertIsNone(unread[1]["announced_at"])


if __name__ == "__main__":
    unittest.main()
