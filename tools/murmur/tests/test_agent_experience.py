"""Agent workflows: valid selectors, bounded context and the normal send journal."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_bookmarks as fixtures
from murmur import __main__ as cli, hooks
from murmur.backend import Backend, BackendError
from murmur.coordinator import routing_binding


class AgentExperienceTests(unittest.TestCase):
    setUp = fixtures.BookmarkTests.setUp
    tearDown = fixtures.BookmarkTests.tearDown

    def run_cli(self, *arguments, stdin=None, output=None):
        output = io.StringIO() if output is None else output
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture",
                    "delivery_enabled": True, "busy_context_enabled": True}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch("murmur.native.current_identity", return_value=self.identity), \
                patch.object(sys, "stdin", io.StringIO(stdin or "")), redirect_stdout(output):
            cli.main(list(arguments))
        return json.loads(output.getvalue())

    def receipts(self):
        return [tuple(row) for row in self.ledger.db.execute("SELECT * FROM receipts ORDER BY arrival")]

    def test_invalid_read_selectors_fail_even_when_a_reply_is_unread_or_already_read(self):
        self.backend.add("root")
        self.backend.add("reply", root="root", when=102)
        self.backend.chats.append({"id": "api", "name": "API", "kind": "channel", "role": "observer"})
        self.backend.add("other-root", channel="api", when=103)
        self.coordinator.sync(self.actor)
        for read_reply in (False, True):
            if read_reply:
                self.ledger.mark_read(self.actor, ["reply"])
            before = self.receipts()
            for channel, root, error in (("missing", None, ValueError), ("build", "missing", BackendError),
                                         ("build", "reply", BackendError), ("build", "other-root", ValueError)):
                with self.subTest(read_reply=read_reply, channel=channel, root=root), self.assertRaises(error):
                    self.coordinator.read(self.actor, channel, root=root)
                self.assertEqual(self.receipts(), before)
        self.ledger.mark_read(self.actor, ["root"])
        self.assertEqual(self.coordinator.read(self.actor, "build", root="root"), [])

    def test_history_pages_have_an_explicit_root_and_invalid_cursors_are_actionable(self):
        self.backend.add("root")
        for i in range(1, 6):
            self.backend.add(f"reply-{i}", root="root", when=101+i)
        self.backend.add("other-root", when=110)
        self.coordinator.sync(self.actor)
        before = self.receipts()
        page = self.coordinator.history(self.actor, "build", "root", limit=2)
        self.assertEqual(page["root"]["id"], "root")
        self.assertEqual([p["id"] for p in page["posts"]], ["reply-4", "reply-5"])
        self.assertEqual(page["next_before"], "reply-4")
        older = self.coordinator.history(self.actor, "build", "root", 2, page["next_before"])
        self.assertEqual([p["id"] for p in older["posts"]], ["reply-2", "reply-3"])
        for cursor in ("other-root", "missing"):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(BackendError, "does not belong to this thread"):
                self.coordinator.history(self.actor, "build", "root", before=cursor)
        with self.assertRaisesRegex(RuntimeError, "another conversation"):
            self.coordinator.history(self.actor, "other-chat", "root", before="root")
        self.assertEqual(self.receipts(), before)

    def test_exact_old_anchor_context_is_bounded_nonconsuming_and_keeps_stars(self):
        self.backend.add("root")
        self.backend.add("old-anchor", root="root", when=102)
        for i in range(3, 7):
            self.backend.add(str(i), root="root", when=100+i)
        self.coordinator.sync(self.actor)
        self.coordinator.set_saved(self.actor, "old-anchor", True)
        before = self.receipts()
        result = self.run_cli("show", "old-anchor", "--context", "2")
        self.assertEqual(result["post"]["id"], "old-anchor")
        self.assertEqual(result["context"]["root"]["id"], "root")
        self.assertEqual([p["id"] for p in result["context"]["posts"]], ["5", "6"])
        self.assertEqual(result["context"]["next_before"], "5")
        self.assertEqual(self.receipts(), before)
        self.assertEqual(self.backend.bookmarks[self.actor], {"old-anchor"})

    def test_thread_summaries_count_all_unread_receipts_without_consuming_them(self):
        self.backend.add("root")
        for i in range(1000):
            self.backend.add("r"+str(i), root="root", when=102+i)
        self.coordinator.sync(self.actor)
        before = self.receipts()
        self.backend.threads = lambda *_args: {"conversation": self.backend.descriptor(self.actor, "build"),
            "threads": [{"id": "root", "channel_id": "build",
            "preview": "Question", "reply_count": 1000}], "next_before": None}
        page = self.coordinator.threads(self.actor, "build", 1)
        self.assertEqual(page["threads"][0]["unread_count"], 1001)
        self.assertNotIn("message", page["threads"][0])
        self.assertEqual(self.receipts(), before)

    def test_reply_uses_actual_destination_causality_and_persisted_uncertainty(self):
        self.backend.add("root", causal={"chain_ids": ["exchange"]})
        self.backend.add("anchor", root="root", when=102, causal={"chain_ids": ["exchange"]})
        self.coordinator.sync(self.actor)
        self.coordinator.set_saved(self.actor, "anchor", True)
        attempt = self.ledger.create_attempt(self.actor, routing_binding(self.identity))
        self.ledger.admit(attempt["token"], self.actor, routing_binding(self.identity), "actual-turn")
        before = self.receipts()
        self.backend.lose_response = True
        with self.assertRaises(BackendError):
            self.coordinator.reply(self.actor, "anchor", "A useful answer")
        self.assertEqual(len(self.backend.mutation_calls), 1)
        payload = self.backend.mutation_calls[0][3]
        self.assertEqual((payload["channel_id"], payload["root_id"], payload["message"]),
                         ("build", "root", "A useful answer"))
        self.assertEqual(payload["causal"], {"chain_ids": ["exchange"]})
        self.assertEqual(self.ledger.outbounds(self.actor)[0]["state"], "uncertain")
        self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(len(self.backend.mutation_calls), 1)
        self.assertEqual(self.ledger.outbounds(self.actor)[0]["state"], "complete")
        self.assertEqual(self.receipts(), before)
        self.assertEqual(self.backend.bookmarks[self.actor], {"anchor"})

    def test_stdin_file_and_literal_reply_preserve_text_and_normal_journal(self):
        self.backend.add("root")
        text = 'Unicode λ\n`code` "$literal" \'quoted\'\n$(not a shell)\n'
        source = Path(self.home.name) / "message.txt"
        source.write_text(text, encoding="utf-8")
        for arguments, stdin in ((("send", "build", "--text-file", "-"), text),
                                 (("reply", "root", "--text-file", str(source)), None),
                                 (("reply", "root", text), None)):
            result = self.run_cli(*arguments, stdin=stdin)
            self.assertEqual(result["post"]["message"], text)
        self.assertEqual([call[3]["root_id"] for call in self.backend.mutation_calls], ["", "root", "root"])
        self.assertEqual(len(self.ledger.outbounds(self.actor)), 3)
        for text in ("   ", "a" * 16001):
            before = len(self.backend.mutation_calls)
            with self.assertRaisesRegex(ValueError, "16,000"):
                self.run_cli("reply", "root", "--text-file", "-", stdin=text)
            self.assertEqual(len(self.backend.mutation_calls), before)

    def test_local_discovery_is_bounded_and_ambiguous_names_never_choose_a_peer(self):
        for label in ("b", "c", "d"):
            self.ledger.enroll("codex:"+label, {"session_id": label}, "Same Title", "user-"+label)
        result = self.run_cli("agents", "--scope", "local", "--query", "sAmE", "--limit", "2")
        self.assertEqual([a["id"] for a in result["agents"]], ["codex:b", "codex:c"])
        self.assertEqual(result["next_after"], "codex:c")
        self.assertEqual(result["scope"], "local")
        page = self.run_cli("agents", "--scope", "local", "--query", "sAmE", "--limit", "2", "--after", result["next_after"])
        self.assertEqual([a["id"] for a in page["agents"]], ["codex:d"])
        self.assertIsNone(page["next_after"])
        with self.assertRaisesRegex(ValueError, "cursor no longer matches"):
            self.run_cli("agents", "--scope", "local", "--query", "no-match", "--after", "codex:c")
        with self.assertRaisesRegex(ValueError, "names may repeat"):
            self.run_cli("dm", "Same Title")
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.ledger.outbounds(self.actor), [])
        chat = self.run_cli("chats")[0]
        self.assertIs(type(chat["muted"]), bool)
        self.assertIs(type(chat["mute_explicit"]), bool)
        self.assertNotIn("available", chat)

    def test_help_and_input_failures_need_no_identity_or_config(self):
        with patch.object(cli.config, "load", side_effect=AssertionError("No config for help")), \
                patch("murmur.native.current_identity", side_effect=AssertionError("No identity for help")):
            for command, semantics in (("read", "consume"), ("history", "next_before"),
                                       ("send", "UTF-8"), ("reply", "root"), ("agents", "names may repeat")):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as result:
                    cli.main([command, "--help"])
                self.assertEqual(result.exception.code, 0)
                self.assertIn(semantics.casefold(), output.getvalue().casefold())

    def test_transport_forwards_page_bounds_and_exact_thread_cursor(self):
        calls = []
        backend = Backend("http://fixture.invalid", "fixture")
        backend.request = lambda *args, **query: calls.append((args, query)) or {"agents": [], "posts": []}
        backend.agent_page("same title", 2, "codex:b")
        backend.thread_page(self.actor, "root", 2, "reply")
        backend.threads(self.actor, "build", 3, "old-root")
        self.assertEqual(calls[0], (("GET", "/agents"), {"query": "same title", "limit": 2, "after": "codex:b"}))
        self.assertEqual(calls[1][1], {"actor": self.actor, "limit": 2, "before": "reply"})
        self.assertEqual(calls[2][1], {"actor": self.actor, "limit": 3, "before": "old-root"})

    def test_doctor_separates_support_gates_failed_work_and_grouped_historical_errors(self):
        self.backend.request = lambda *_args: {"version": "fixture", "reconciliation_error": ""}
        for _ in range(3):
            hooks.record_error(self.ledger, self.actor, "hook", "Unsupported native profile")
        failed = self.ledger.prepare_outbound(self.actor, {"action": "post", "channel_id": "build", "message": "Denied"})
        self.ledger.set_outbound(failed["op_id"], "failed", error="Posting denied")
        pending = self.ledger.prepare_outbound(self.actor, {"action": "post", "channel_id": "build", "message": "Unresolved"})
        from murmur import diagnostics
        with patch.object(diagnostics.native, "verify_target", return_value=self.identity), \
                patch.object(diagnostics, "route_capabilities", return_value={
                    "idle_queue": {"status": "unverified", "reason": "Owner unknown"},
                    "busy_context": {"status": "unsupported", "reason": "Uncertified profile"}}):
            result = self.run_cli("doctor")
        self.assertTrue(result["native"]["ok"])
        self.assertEqual(result["routes"]["busy_context"]["status"], "unsupported")
        self.assertIn("no current coordinator lease for this actor", result["routes"]["busy_context"]["blocked_by"])
        self.assertEqual([o["op_id"] for o in result["operations"]], [pending["op_id"]])
        self.assertEqual([o["op_id"] for o in result["failed_operations"]], [failed["op_id"]])
        self.assertEqual(result["operation_counts"], {"failed": 1, "pending": 1})
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["count"], 3)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM diagnostics").fetchone()[0], 3)

    def test_doctor_keeps_uncertain_idle_and_emitted_context_recovery_bindings(self):
        self.backend.request = lambda *_args: {"version": "fixture", "reconciliation_error": ""}
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        idle = self.ledger.create_attempt(self.actor, "owner", token="uncertain-idle-token")
        self.ledger.set_transport(idle["token"], "uncertain", {"detail": "No producer receipt"})
        from murmur import diagnostics
        routes = {"idle_queue": {"status": "unverified"}, "busy_context": {"status": "unverified"}}
        with patch.object(diagnostics, "route_capabilities", return_value=routes), \
                patch.object(diagnostics.native, "verify_target", return_value=self.identity):
            result = self.run_cli("doctor")
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual((result["attempts"][0]["token"], result["attempts"][0]["transport"]),
                             ("uncertain-idle-token", "uncertain"))
            self.assertIsNone(result["active_turn"])
            self.ledger.retire_attempt(idle["token"], "Isolated test retirement")
            self.backend.add("later-request", when=102)
            self.coordinator.sync(self.actor)
            identity = self.ledger.actor(self.actor)["identity"]
            self.ledger.start_turn(self.actor, "owner", "existing-turn", native_identity=identity)
            context = self.ledger.claim_context(self.actor, "owner", "existing-turn", "tool-1", native_identity=identity)
            self.assertEqual(context["post_ids"], ["later-request"])
            self.ledger.mark_context_emitted(context["token"], self.actor, "owner", "existing-turn", "tool-1")
            before = self.ledger.attempt(context["token"])
            result = self.run_cli("doctor")
        self.assertEqual(result["attempt_count"], 1)
        attempt = result["attempts"][0]
        self.assertEqual((attempt["token"], attempt["mode"], attempt["status"], attempt["expected_turn_key"]),
                         (context["token"], "context", "pending", "existing-turn"))
        self.assertIsNotNone(attempt["emitted_at"])
        self.assertFalse(attempt["verification_present"])
        self.assertNotIn("automatic_budget", result)
        self.assertFalse(any("budget" in reason for route in result["routes"].values()
                             for reason in route["blocked_by"]))
        self.assertEqual(self.ledger.attempt(context["token"]), before)

    def test_exact_read_skips_old_same_thread_work_and_acknowledges_only_after_flush(self):
        self.backend.add("root")
        for i in range(25):
            self.backend.add(f"old-{i}", root="root", when=102+i)
        self.backend.add("fresh-request", root="root", when=200)
        self.backend.add("other-request", when=201)
        self.coordinator.sync(self.actor)
        self.coordinator.set_saved(self.actor, "fresh-request", True)
        self.assertEqual(self.run_cli("show", "fresh-request")["post"]["id"], "fresh-request")

        class BrokenOutput(io.StringIO):
            def flush(self):
                raise BrokenPipeError("Output was not delivered")

        with self.assertRaises(BrokenPipeError):
            self.run_cli("read", "--post", "fresh-request", output=BrokenOutput())
        self.assertIsNone(self.ledger.db.execute("SELECT read_at FROM receipts WHERE post_id='fresh-request'").fetchone()[0])
        selected = self.run_cli("read", "--post", "fresh-request")
        self.assertEqual([post["id"] for post in selected["posts"]], ["fresh-request"])
        unread = {post["id"] for post in self.ledger.unread_posts(self.actor, limit=1000)}
        self.assertEqual(unread, {"root", "other-request", *(f"old-{i}" for i in range(25))})
        self.assertEqual(self.backend.bookmarks[self.actor], {"fresh-request"})
        with self.assertRaisesRegex(ValueError, "tracked accessible unread"):
            self.run_cli("read", "--post", "fresh-request")

    def test_exact_read_selector_errors_do_not_load_config_or_identity(self):
        with patch.object(cli.config, "load", side_effect=AssertionError("No config for invalid selectors")), \
                patch("murmur.native.current_identity", side_effect=AssertionError("No identity for invalid selectors")):
            for arguments in (("read",), ("read", "build", "--post", "request"),
                              ("read", "--post", "request", "--thread", "root"),
                              ("read", "--post", "request", "--limit", "1")):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    cli.main(list(arguments))

    def test_offline_doctor_preserves_local_recovery_and_completed_admission(self):
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        attempt = self.ledger.create_attempt(self.actor, "epoch", token="completed-admission")
        self.ledger.admit(attempt["token"], self.actor, "epoch", "completed-turn")
        self.ledger.complete(self.actor, "completed-turn", "epoch")
        operation = self.ledger.prepare_outbound(self.actor, {"action": "post", "channel_id": "build", "message": "Uncertain"})
        self.ledger.set_outbound(operation["op_id"], "uncertain", error="Lost HTTP response")
        self.backend.request = lambda *_args: (_ for _ in ()).throw(BackendError("Server unavailable"))
        from murmur import diagnostics
        with patch.object(diagnostics.native, "verify_target", return_value=self.identity), \
                patch.object(diagnostics, "route_capabilities", return_value={"idle_queue": {"status": "unverified"}}):
            result = self.run_cli("doctor")
        self.assertEqual(result["server"], {"status": "unavailable", "error": "Server unavailable"})
        self.assertTrue(result["enrollment"]["authorized"])
        self.assertEqual(result["enrollment"]["account"]["status"], "provisioned")
        self.assertEqual([item["op_id"] for item in result["operations"]], [operation["op_id"]])
        self.assertEqual(result["last_admission"]["token"], "completed-admission")
        self.assertEqual(result["last_admission"]["status"], "completed")
        self.assertEqual(result["attempt_count"], 0)
        self.identity = replace(self.identity, session_id="44444444-4444-4444-8444-444444444444", parent_known=False)
        with patch.object(diagnostics.native, "verify_target") as verify, \
                patch.object(diagnostics, "route_capabilities", return_value={"idle_queue": {"status": "unverified"}}):
            result = self.run_cli("doctor")
        verify.assert_not_called()
        self.assertFalse(result["enrolled"])
        self.assertEqual(result["native"]["ancestry_status"], "not_checked")
        self.assertEqual(result["enrollment"]["account"]["status"], "not_requested")
        self.assertEqual(result["server"]["status"], "unavailable")

    def test_doctor_separates_authorization_provisioning_and_unenrolled_parent(self):
        self.identity = replace(self.identity, session_id="22222222-2222-4222-8222-222222222222",
                                parent_session_id="33333333-3333-4333-8333-333333333333")
        with patch.object(self.backend, "register", side_effect=BackendError("Provisioning rejected", status=403)):
            with self.assertRaises(BackendError):
                self.coordinator.register(self.identity, "Exact native title")
        self.backend.request = lambda *_args: {"version": "fixture"}
        from murmur import diagnostics
        with patch.object(diagnostics.native, "verify_target", return_value=self.identity), \
                patch.object(diagnostics, "route_capabilities", return_value={"idle_queue": {"status": "unverified"}}):
            result = self.run_cli("doctor")
        self.assertTrue(result["enrollment"]["authorized"])
        self.assertEqual(result["enrollment"]["account"], {"status": "pending", "user_id": None})
        self.assertTrue(result["native"]["parent_known"])
        self.assertFalse(result["enrollment"]["parent"]["authorized_locally"])
        self.assertIn("server account not provisioned", result["routes"]["idle_queue"]["blocked_by"])

    def test_health_probe_is_bounded_and_registration_forwards_verified_edges(self):
        backend = Backend("http://fixture.invalid", "fixture", timeout=15)
        with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b'{"version":"fixture"}')) as request:
            backend.request("GET", "/health")
            self.assertEqual(request.call_args.kwargs["timeout"], 2)
            backend.request("GET", "/agents")
            self.assertEqual(request.call_args.kwargs["timeout"], 15)
        edges = [{"actor": self.actor, "parent": None}]
        with patch.object(backend, "request", return_value={}) as request:
            backend.register(self.actor, "Exact title", enrollment_id="local-enrollment", lineage=edges, mode="native")
        self.assertEqual(request.call_args.args[2], {"actor": self.actor, "name": "Exact title", "parent": None,
                                                    "enrollment_id": "local-enrollment", "lineage": edges, "mode": "native"})
        with patch.object(backend, "request", return_value={}) as request:
            backend.register(self.actor, "Manual title", mode="manual")
        self.assertEqual(request.call_args.args[2], {"actor": self.actor, "name": "Manual title", "parent": None, "mode": "manual"})
        with self.assertRaises(TypeError):
            backend.register(self.actor, "Ambiguous mode")
        with patch.object(backend, "request", return_value={}) as request:
            backend.register(self.actor, "No invented proof", mode="native")
        self.assertNotIn("lineage", request.call_args.args[2])

    def test_health_compatibility_does_not_claim_support_from_an_old_or_missing_protocol(self):
        from murmur import diagnostics
        for version, expected in ((3, "compatible"), (2, "incompatible"), (1, "incompatible"), (None, "unverified")):
            self.backend.request = lambda *_args: {"build": {"compatibility": {"attention_protocol": version}}}
            with self.subTest(version=version):
                result = diagnostics.server_health(self.backend)
                self.assertEqual(result["status"], "available")
                self.assertEqual(result["compatibility"]["status"], expected)
                self.assertEqual(result["compatibility"]["required_attention_protocol"], 3)
        for value in ([], {"build": None}, {"build": {"compatibility": "unknown"}}):
            self.backend.request = lambda *_args: value
            with self.subTest(value=value):
                self.assertEqual(diagnostics.server_health(self.backend)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
