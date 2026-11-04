"""Busy hook/coordinator integration with real SQLite and a fake native boundary."""

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
