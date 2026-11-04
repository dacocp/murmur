"""Agent-initiated operator DMs through the real CLI/client and isolated SQLite."""

from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity


class OperatorDMClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="murmur-operator-dm-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.identity = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
        self.actor = self.identity.key
        self.settings = {"url": "http://fixture.invalid", "token": "fixture-coordinator-token"}
        self.chat = {"id": "operator-pair", "name": "Operator conversation", "kind": "dm", "role": "member"}
        self.result = {"conversation": {key: self.chat[key] for key in ("id", "name", "kind")}}
        with closing(Ledger(self.path)) as ledger:
            ledger.enroll(self.actor, asdict(self.identity), "Exact native task title", "self-user")

    @staticmethod
    def response(value):
        return io.BytesIO(json.dumps(value).encode())

    @staticmethod
    def missing(request):
        return HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"Not found"}'))

    def invoke(self, arguments, exchange, *, identity=None):
        output = io.StringIO()
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value=self.settings), \
                patch("murmur.native.current_identity", return_value=identity or self.identity), \
                patch("urllib.request.urlopen", side_effect=exchange), redirect_stdout(output):
            cli.main(arguments)
        return json.loads(output.getvalue())

    def inspect_intent(self, request, action="operator_dm", payload=None, *, causal=None):
        self.assertEqual(request.get_method(), "POST")
        endpoint = "operator-dm" if action == "operator_dm" else "conversations"
        self.assertEqual(request.full_url, self.settings["url"] + "/plugins/murmur/v1/" + endpoint)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.settings["token"])
        body = json.loads(request.data)
        operation_id = body["op_id"]
        self.assertTrue(operation_id)
        self.assertEqual(body, {"actor": self.actor, "op_id": operation_id, **(payload or {})})
        # A second connection observes committed intent before the HTTP boundary.
        with closing(Ledger(self.path)) as ledger:
            operation = ledger.outbound(operation_id)
            self.assertEqual(operation["actor"], self.actor)
            self.assertEqual(operation["state"], "pending")
            self.assertEqual(operation["payload"], {"action": action, **(payload or {}),
                                                    **({"causal": causal} if causal else {})})
        return operation_id

    def test_operator_cli_binds_current_actor_and_commits_intent_before_http(self):
        causal = {"chain_ids": ["origin:prior-request"]}
        with closing(Ledger(self.path)) as ledger:
            ledger.db.execute("UPDATE actors SET causal_json=? WHERE actor=?",
                              (json.dumps(causal["chain_ids"]), self.actor))
        calls = []
        def exchange(request, **options):
            calls.append(self.inspect_intent(request, causal=causal))
            return self.response(self.result)
        self.assertEqual(self.invoke(["dm", "--operator"], exchange), self.result)
        self.assertEqual(len(calls), 1)
        with closing(Ledger(self.path)) as ledger:
            operation = ledger.outbound(calls[0])
            self.assertEqual((operation["state"], operation["result"]),
                             ("complete", {"channel_id": self.chat["id"]}))
            self.assertEqual([actor["actor"] for actor in ledger.actors()], [self.actor])

    def test_recipient_errors_need_no_state_identity_config_or_http(self):
        cases = [(["dm"], SystemExit), (["dm", "--operator", "codex:peer"], SystemExit),
                 (["dm", "claude:peer", "--operator"], SystemExit), (["dm", "human-name"], ValueError),
                 (["dm", "human:operator"], ValueError), (["dm", "--operator", "human-name"], SystemExit),
                 (["dm", "--operator", "--actor", "codex:other"], SystemExit),
                 (["dm", "--operator", "--username", "human-name"], SystemExit),
                 (["dm", "--operator", "--author", "codex:other"], SystemExit)]
        with patch.object(cli, "Ledger", side_effect=AssertionError("No state for invalid input")), \
                patch.object(cli, "Backend", side_effect=AssertionError("No backend for invalid input")), \
                patch.object(cli.config, "load", side_effect=AssertionError("No config for invalid input")), \
                patch("murmur.native.current_identity", side_effect=AssertionError("No native inspection")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No HTTP for invalid input")), \
                redirect_stderr(io.StringIO()):
            for arguments, error_type in cases:
                with self.subTest(arguments=arguments), self.assertRaises(error_type) as error:
                    cli.main(arguments)
                if error_type is SystemExit:
                    self.assertEqual(error.exception.code, 2)
                else:
                    self.assertIn("peer actor IDs", str(error.exception))

    def test_unenrolled_session_cannot_use_another_sessions_operator_dm(self):
        other = replace(self.identity, session_id="22222222-2222-4222-8222-222222222222")
        with self.assertRaises(LedgerError):
            self.invoke(["dm", "--operator"], lambda *_args, **_kwargs: self.fail("Unenrolled HTTP"), identity=other)
        with closing(Ledger(self.path)) as ledger:
            self.assertEqual(ledger.outbounds(), [])
            self.assertEqual([actor["actor"] for actor in ledger.actors()], [self.actor])

    def test_normal_peer_dm_and_channel_keep_their_existing_wire_contract(self):
        peer = "claude:22222222-2222-4222-8222-222222222222"
        for arguments, payload in ((["dm", peer], {"kind": "dm", "name": "", "members": [peer]}),
                (["channel", "Review", peer, "--purpose", "Review the change"],
                 {"kind": "channel", "name": "Review", "members": [peer], "purpose": "Review the change"})):
            def exchange(request, **options):
                self.inspect_intent(request, "conversation", payload)
                return self.response(self.result)
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(arguments, exchange), self.result)

    def test_old_server_missing_route_fails_visibly_without_fallback(self):
        calls = []
        def exchange(request, **options):
            calls.append(self.inspect_intent(request))
            raise self.missing(request)
        with self.assertRaises(BackendError) as error:
            self.invoke(["dm", "--operator"], exchange)
        self.assertEqual(error.exception.status, 404)
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(len(calls), 1)
        with closing(Ledger(self.path)) as ledger:
            self.assertEqual(ledger.outbound(calls[0])["state"], "failed")

    def test_lost_response_reconciles_owned_operation_without_reposting(self):
        for committed in (True, False):
            calls, operation_id = [], None
            def exchange(request, **options):
                nonlocal operation_id
                calls.append(request.get_method())
                if request.get_method() == "POST":
                    operation_id = self.inspect_intent(request)
                    raise OSError("Response lost after request write")
                parts = urlsplit(request.full_url)
                self.assertEqual(parts.path, "/plugins/murmur/v1/operations/" + operation_id)
                self.assertEqual(parse_qs(parts.query), {"actor": [self.actor]})
                if not committed:
                    raise self.missing(request)
                return self.response({"status": "complete", "result": {"channel_id": self.chat["id"]}})
            with self.subTest(committed=committed), patch("urllib.request.urlopen", side_effect=exchange):
                with self.assertRaises(BackendError) as error:
                    self.invoke(["dm", "--operator"], exchange)
                self.assertTrue(error.exception.uncertain)
                # Reopen persisted state after the failed CLI invocation.
                with closing(Ledger(self.path)) as ledger:
                    self.assertEqual(ledger.outbound(operation_id)["state"], "uncertain")
                    coordinator = Coordinator(ledger, Backend(**self.settings))
                    coordinator.reconcile_outbound(self.actor)
                    coordinator.reconcile_outbound(self.actor)
                    operation = ledger.outbound(operation_id)
                    self.assertEqual(operation["state"], "complete" if committed else "uncertain")
                    self.assertEqual(operation["result"], {"channel_id": self.chat["id"]} if committed else None)
                self.assertEqual(calls, ["POST", "GET"] if committed else ["POST", "GET", "GET"])

    def test_completed_operator_pair_uses_normal_feed_and_preserves_explicit_mute(self):
        sequence = 1
        def exchange(request, **options):
            if request.get_method() == "POST":
                self.inspect_intent(request)
                return self.response(self.result)
            self.assertEqual(urlsplit(request.full_url).path, "/plugins/murmur/v1/changes")
            return self.response({"attention_version": 3, "conversations": [self.chat], "generation": 1,
                "cursor": sequence, "reset": sequence == 1, "more": False,
                "changes": [{"seq": sequence, "first_seen_seq": sequence, "notification_eligible": True,
                    "post": {"id": "human-request-" + str(sequence), "channel_id": self.chat["id"],
                             "user_id": "operator-user", "root_id": "", "create_at": 100 + sequence,
                             "update_at": 100 + sequence, "message": "Please review", "author": "Human"}}]})
        with patch("urllib.request.urlopen", side_effect=exchange):
            self.invoke(["dm", "--operator"], exchange)
            with closing(Ledger(self.path)) as ledger:
                coordinator = Coordinator(ledger, Backend(**self.settings))
                coordinator.sync(self.actor)
                chat = ledger.conversations(self.actor)[0]
                self.assertEqual((chat["kind"], chat["role"], chat["muted"]), ("dm", "member", 0))
                self.assertTrue(ledger.unread_posts(self.actor)[0]["notification_eligible"])
                ledger.set_mute(self.actor, self.chat["id"], True)
            sequence = 2
            self.invoke(["dm", "--operator"], exchange)
            with closing(Ledger(self.path)) as ledger:
                Coordinator(ledger, Backend(**self.settings)).sync(self.actor)
                chat = ledger.conversations(self.actor)[0]
                self.assertEqual((chat["muted"], chat["mute_explicit"]), (1, 1))
                unread = ledger.unread_posts(self.actor)
                self.assertEqual([post["id"] for post in unread], ["human-request-1", "human-request-2"])
                self.assertTrue(all(not post["notification_eligible"] and post["read_at"] is None for post in unread))


if __name__ == "__main__":
    unittest.main()
