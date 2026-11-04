"""Hook entry points with isolated state and actual SQLite admission failures."""

from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli
from murmur import hooks
from murmur.coordinator import routing_binding
from murmur.ledger import Ledger
from murmur.native import HookEvent, NativeIdentity, format_doorbell
from murmur.native.lineage import observation


SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "codex:22222222-2222-4222-8222-222222222222"
TOKEN = "fixture-entrypoint-token"


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="murmur-entrypoint-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "fresh-runtime"
        self.path = self.home / "operations.sqlite3"
        self.identity = NativeIdentity("codex", SESSION, parent_known=True,
                                       target={"native_epoch": "fixture-generation"})

    def invoke(self, arguments, payload):
        output = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()), encoding="utf-8")
        with patch.dict(os.environ, {"MURMUR_HOME": str(self.home)}), \
                patch.object(sys, "stdin", stdin), redirect_stdout(output), \
                patch.object(cli, "Backend", side_effect=AssertionError("Hook must not construct a backend")) as backend, \
                patch.object(cli.config, "load", side_effect=AssertionError("Hook must not read credentials")) as load, \
                patch("urllib.request.urlopen", side_effect=AssertionError("Hook must not perform HTTP")) as http:
            cli.main(arguments)
            backend.assert_not_called()
            load.assert_not_called()
            http.assert_not_called()
        return json.loads(output.getvalue())

    def prepare_mail(self):
        with self.open_ledger() as ledger:
            hooks.initialize(ledger.db)
            ledger.enroll(self.identity.key, asdict(self.identity), "Murmur fixture", "self-user")
            ledger.sync_conversations(self.identity.key, [{"id": "build", "name": "Build review",
                "kind": "channel", "role": "member"}])
            ledger.ingest(self.identity.key, [{"id": "request", "channel_id": "build",
                "user_id": "peer-user", "author": "Bea", "root_id": "", "create_at": 101,
                "update_at": 101, "causal": "fixture-exchange"}], cursor=1, generation=1)
            self.assertIsNotNone(ledger.create_attempt(self.identity.key,
                                                      routing_binding(self.identity), TOKEN))

    def open_ledger(self):
        # Ledger intentionally exposes close(), rather than a context protocol.
        from contextlib import closing
        return closing(Ledger(self.path, clock=lambda: 100))

    def submit_event(self):
        prompt = format_doorbell(["Build review"], TOKEN)
        return HookEvent(self.identity, "submit", "UserPromptSubmit", TURN,
                         prompt=prompt, wake_token=TOKEN)

    def test_configured_first_session_start_captures_creation_without_enrollment_or_http(self):
        self.home.mkdir()
        (self.home / "config.json").write_text('{"url":"http://unused.invalid","token":"fixture"}')
        self.assertFalse(self.path.exists())
        identity = NativeIdentity("claude", SESSION)
        creation = observation({"session_id": SESSION, "hook_event_name": "SessionStart", "source": "startup"},
                               {"pid": 4242, "proc_start": "fixture-process-birth", "entrypoint": "cli",
                                "version": "2.1.310", "root_start_verified": True})
        event = HookEvent(identity, "session_start", "SessionStart", creation=creation)
        with patch("murmur.native.parse_hook", return_value=event) as parse:
            response = self.invoke(["hook", "claude", "SessionStart"],
                                   {"session_id": SESSION, "hook_event_name": "SessionStart"})
        self.assertEqual(response, {})
        parse.assert_called_once()
        self.assertTrue(self.path.is_file())
        with self.open_ledger() as ledger:
            observations = ledger.db.execute("SELECT evidence FROM native_observations").fetchall()
            self.assertEqual([json.loads(row[0]) for row in observations], [creation])
            self.assertEqual(ledger.actors(), [])
            self.assertEqual(ledger.db.execute("SELECT count(*) FROM native_proofs").fetchone()[0], 0)
            self.assertEqual(ledger.attempts(), [])

    def test_enrolled_compaction_reminder_uses_local_state_without_credentials_or_http(self):
        self.prepare_mail()
        with self.open_ledger() as ledger:
            ledger.clock = lambda: time.time_ns() // 1_000_000
            hooks.publish_control(ledger, False, delivery_enabled=True)
        event = HookEvent(self.identity, "session_start", "SessionStart", source="compact")
        with patch("murmur.native.parse_hook", return_value=event):
            response = self.invoke(["hook", "codex", "SessionStart"],
                                   {"hook_event_name": "SessionStart", "source": "compact"})
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "SessionStart")
        with self.open_ledger() as ledger:
            self.assertEqual(ledger.attempt(TOKEN)["status"], "pending")
            self.assertIsNone(ledger.attempt(TOKEN)["agent_seen_at"])
            self.assertIsNone(ledger.unread_posts(self.identity.key)[0]["read_at"])

    def test_absent_ledger_blocks_a_valid_doorbell_without_creating_state(self):
        event = self.submit_event()
        with patch.object(cli, "Ledger", side_effect=AssertionError("Absent state must stay absent")) as sql, \
                patch("murmur.native.parse_hook", side_effect=AssertionError("No ledger admission is possible")) as parse:
            response = self.invoke(["hook", "codex", "UserPromptSubmit"],
                                   {"hook_event_name": "UserPromptSubmit", "prompt": event.prompt,
                                    "session_id": SESSION, "turn_id": TURN.split(":", 1)[1]})
        sql.assert_not_called()
        parse.assert_not_called()
        self.assertEqual(response["decision"], "block")
        self.assertIn("ledger is absent", response["reason"])
        self.assertFalse(self.home.exists())

    def test_absent_ledger_ordinary_submit_is_inert_even_with_a_quoted_marker(self):
        with patch.object(cli, "Ledger", side_effect=AssertionError("Do not create implicit state")) as sql, \
                patch("murmur.native.parse_hook", side_effect=AssertionError("No passive home was configured")) as parse:
            response = self.invoke(["hook", "codex", "UserPromptSubmit"],
                                   {"hook_event_name": "UserPromptSubmit", "session_id": SESSION,
                                    "prompt": "Explain the literal [wake:example] in this source file."})
        sql.assert_not_called()
        parse.assert_not_called()
        self.assertEqual(response, {})
        self.assertFalse(self.home.exists())

    def test_real_write_lock_holds_admission_within_the_hook_bound(self):
        self.prepare_mail()
        event = self.submit_event()
        blocker = sqlite3.connect(self.path, timeout=0.2, isolation_level=None)

        def open_then_lock(path, *, timeout):
            self.assertEqual(timeout, 0.2)
            ledger = Ledger(path, timeout=timeout)
            # Acquire the real competing write lock after the CLI connection's
            # schema setup, so admission itself meets the blocked writer.
            blocker.execute("BEGIN IMMEDIATE")
            self.assertTrue(blocker.in_transaction)
            return ledger

        try:
            with patch.object(cli, "Ledger", side_effect=open_then_lock), \
                    patch("murmur.native.parse_hook", return_value=event) as parse:
                started = time.monotonic()
                response = self.invoke(["hook", "codex", "UserPromptSubmit"],
                                       {"hook_event_name": "UserPromptSubmit", "prompt": event.prompt})
                elapsed = time.monotonic() - started
            parse.assert_called_once()
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(response["decision"], "block")
        self.assertIn("locked", response["reason"])
        self.assertLess(elapsed, 2.0, "Hook must not wait on the ordinary five-second SQLite timeout")
        with self.open_ledger() as ledger:
            self.assertEqual(ledger.attempt(TOKEN)["status"], "pending")
            self.assertIsNone(ledger.active_turn(self.identity.key))
            self.assertIsNone(ledger.unread_posts(self.identity.key)[0]["announced_at"])

    def test_retired_stop_form_is_inert_before_config_sql_or_backend_access(self):
        arguments = ["hook", "--client", "codex", "--event", "Stop",
                     "--home", "/nonexistent/retired", "--scope-file", "/nonexistent/retired/scope.json"]
        with patch.object(cli, "Ledger", side_effect=AssertionError("Retired hook must not open SQLite")) as sql, \
                patch.object(cli.config, "home", side_effect=AssertionError("Retired hook must not select state")) as home, \
                patch.object(cli.config, "ledger_path", side_effect=AssertionError("Retired hook must not access state")) as path, \
                patch("sqlite3.connect", side_effect=AssertionError("Retired hook must not connect to SQLite")) as connect:
            self.assertEqual(self.invoke(arguments, {}), {})
        for dependency in (sql, home, path, connect):
            dependency.assert_not_called()
        process = subprocess.run([sys.executable, "-m", "murmur", *arguments],
            env={**os.environ, "MURMUR_HOME": str(self.home),
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            input='{}', text=True, capture_output=True, timeout=2)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {})
        self.assertEqual(process.stderr, "")
        self.assertFalse(self.home.exists())

    def test_current_hook_form_admits_the_exact_attempt_and_announces_its_batch(self):
        self.prepare_mail()
        event = self.submit_event()
        with patch("murmur.native.parse_hook", return_value=event) as parse:
            response = self.invoke(["hook", "codex", "UserPromptSubmit"],
                                   {"hook_event_name": "UserPromptSubmit", "prompt": event.prompt})
        parse.assert_called_once()
        self.assertEqual(response, {})
        with self.open_ledger() as ledger:
            self.assertEqual(ledger.attempt(TOKEN)["status"], "admitted")
            self.assertEqual(ledger.active_turn(self.identity.key)["turn_key"], TURN)
            unread = ledger.unread_posts(self.identity.key)
            self.assertEqual([post["id"] for post in unread], ["request"])
            self.assertIsNotNone(unread[0]["announced_at"])
            self.assertIsNone(unread[0]["read_at"])

    def test_lost_busy_stdout_is_held_without_reinjection_or_announcement(self):
        self.prepare_mail()
        with self.open_ledger() as ledger:
            ledger.set_transport(TOKEN, "rejected", {"detail": "Idle fixture replaced before any transport"})
            ledger.start_turn(self.identity.key, routing_binding(self.identity), TURN,
                              native_identity=asdict(self.identity))
            ledger.clock = lambda: time.time_ns() // 1_000_000
            hooks.publish_control(ledger, True)
        event = HookEvent(self.identity, "post_tool", "PostToolUse", TURN,
                          tool_id="fixture-tool", transcript_path="/fixture/native-stream")
        with patch("murmur.native.parse_hook", return_value=event), \
                patch("murmur.native.prepare_context_receipt",
                      return_value={"format": "fixture", "offset": 10}), \
                patch.object(cli, "emit", side_effect=BrokenPipeError("Lost hook output")):
            with self.assertRaises(BrokenPipeError):
                self.invoke(["hook", "codex", "PostToolUse"], {"hook_event_name": "PostToolUse"})
        with self.open_ledger() as ledger:
            attempt = [a for a in ledger.attempts() if a["mode"] == "context"][0]
            self.assertEqual(attempt["status"], "pending")
            self.assertIsNotNone(attempt["emitted_at"])
            self.assertIsNone(attempt["verification"])
            self.assertIsNone(ledger.unread_posts(self.identity.key)[0]["announced_at"])
            self.assertEqual(ledger.active_turn(self.identity.key)["turn_key"], TURN)
        with patch("murmur.native.parse_hook", return_value=event), \
                patch("murmur.native.prepare_context_receipt",
                      return_value={"format": "fixture", "offset": 20}):
            self.assertEqual(self.invoke(["hook", "codex", "PostToolUse"], {}), {})
        with self.open_ledger() as ledger:
            self.assertEqual(len([a for a in ledger.attempts() if a["mode"] == "context"]), 1)


if __name__ == "__main__":
    unittest.main()
