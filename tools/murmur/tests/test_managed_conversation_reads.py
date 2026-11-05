"""Managed references stay separate from participation, receipts and doorbells."""

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur import __main__ as cli, hooks
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator, conversation_descriptor, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from test_coordinator import FakeBackend
from test_open_channels import OpenBackend


class ManagedBackend(OpenBackend):
    def __init__(self):
        super().__init__()
        self.enabled = True
        self.directory_calls = []
        self.directory_page = None
        self.download_calls = []

    def descriptor(self, actor, channel):
        value = super().descriptor(actor, channel)
        if channel == "private-channel":
            value.update(open=False, visibility="legacy-private")
        return value

    def authorize(self, actor, channel):
        view = self.descriptor(actor, channel)
        if view["role"] is None and not view["open"] and not self.enabled:
            raise BackendError("Managed reads disabled", status=403)
        return view

    def post(self, actor, post_id):
        post = FakeBackend.post(self, actor, post_id)
        self.authorize(actor, post["channel_id"])
        return post

    def posts(self, actor, channel, limit, before):
        self.authorize(actor, channel)
        return super().posts(actor, channel, limit, before)

    def threads(self, actor, channel, limit, before):
        self.authorize(actor, channel)
        return super().threads(actor, channel, limit, before)

    def conversation_directory(self, actor, query, kind, agent, limit, after):
        self.directory_calls.append((actor, query, kind, agent, limit, after))
        if not self.enabled:
            raise BackendError("Managed reads disabled", status=403)
        return deepcopy(self.directory_page)

    def saved(self, actor):
        result = []
        for post_id in sorted(self.stars.get(actor, set())):
            try:
                post = self.post(actor, post_id)
            except BackendError as error:
                if error.status not in (403, 404):
                    raise
                result.append(dict(post_id=post_id, unavailable=True))
            else:
                result.append(dict(post_id=post_id, post=post,
                                   conversation=self.descriptor(actor, post["channel_id"])))
        return result

    def download(self, actor, post_id, file_id, output):
        post = self.post(actor, post_id)
        if file_id not in post.get("file_ids", []):
            raise BackendError("File is not attached to this post", status=404)
        self.download_calls.append((actor, post_id, file_id, output))
        return dict(post_id=post_id, file_id=file_id, path=output)


class ManagedConversationReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        hooks.initialize(self.ledger.db)
        self.addCleanup(self.ledger.close)
        self.backend = ManagedBackend()
        self.runtime = Coordinator(self.ledger, self.backend)
        self.identity = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
        self.manual = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", parent_known=False)
        self.runtime.register(self.identity, "Native")
        self.runtime.register(self.manual, "Manual", mode="manual")
        self.actor = self.identity.key
        self.backend.chats.extend([dict(id="reference-dm", name="Other agents", kind="dm"),
                                   dict(id="private-channel", name="Private work", kind="channel")])
        self.backend.add("tracked")
        self.backend.add("reference-root", channel="reference-dm", when=102)
        self.backend.add("reference-reply", channel="reference-dm", root="reference-root", when=103)
        self.backend.add("private-root", channel="private-channel", when=104)
        self.backend.canonical["reference-reply"]["file_ids"] = ["image"]
        for identity in (self.identity, self.manual):
            self.runtime.sync(identity.key)
        self.ledger.create_attempt(self.actor, routing_binding(self.identity), "already-pending")
        participants = [dict(kind="agent", id="claude:33333333-3333-4333-8333-333333333333",
                             name="Reviewer", username="reviewer"),
                        dict(kind="human", id="human-user-id", name="David", username="dacocp")]
        self.backend.directory_page = dict(conversations=[
            {**self.backend.descriptor(self.actor, channel), "participants": deepcopy(participants)}
            for channel in ("reference-dm", "private-channel")], next_after=None)

    def state(self):
        return tuple(self.ledger.db.iterdump())

    def run_cli(self, *arguments, identity=None):
        output = io.StringIO()
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch("murmur.native.current_identity", return_value=identity or self.identity), redirect_stdout(output):
            cli.main(list(arguments))
        return json.loads(output.getvalue())

    def test_native_and_manual_discovery_are_quiet_and_normal_chats_stay_focused(self):
        for identity in (self.identity, self.manual):
            with self.subTest(mode=identity == self.manual):
                before, feed_calls = self.state(), list(self.backend.change_calls)
                page = self.run_cli("chats", "--discover", identity=identity)
                self.assertEqual([c["id"] for c in page["conversations"]], ["reference-dm", "private-channel"])
                self.assertTrue(all(c["role"] is None for c in page["conversations"]))
                self.assertEqual(self.backend.directory_calls[-1], (identity.key, "", None, None, 20, None))
                self.assertEqual(self.backend.change_calls, feed_calls)
                self.assertEqual(self.state(), before)
                self.assertEqual({c["id"] for c in self.run_cli("chats", identity=identity)}, {"build", "dm"})
                self.assertNotIn("reference-dm", {c["id"] for c in self.ledger.conversations(identity.key)})
        self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertIsNone(self.ledger.attempt("already-pending")["agent_seen_at"])

    def test_directory_forwards_filters_and_opaque_cursor_without_tracking(self):
        self.backend.directory_page["conversations"] = self.backend.directory_page["conversations"][:1]
        self.backend.directory_page["next_after"] = "opaque+/=cursor"
        before = self.state()
        page = self.run_cli("chats", "--discover", "--query", "review work", "--kind", "dm", "--agent",
                            "claude:33333333-3333-4333-8333-333333333333", "--limit", "1", "--after", "previous+/=")
        self.assertEqual(page["next_after"], "opaque+/=cursor")
        self.assertEqual(self.backend.directory_calls[-1], (self.actor, "review work", "dm",
                         "claude:33333333-3333-4333-8333-333333333333", 1, "previous+/="))
        self.assertEqual(self.state(), before)
        for arguments in (("--query", "work"), ("--kind", "dm"), ("--agent", self.actor),
                          ("--limit", "20"), ("--after", "cursor")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "--discover"):
                self.run_cli("chats", *arguments)

    def test_reference_content_and_download_never_create_receipts_or_membership(self):
        for actor in (self.actor, self.manual.key):
            before, feed_calls = self.state(), list(self.backend.change_calls)
            self.assertEqual(self.runtime.show(actor, "reference-reply")["id"], "reference-reply")
            self.assertEqual(self.runtime.history(actor, "reference-dm", "reference-root")["root"]["id"], "reference-root")
            self.assertEqual(self.runtime.context(actor, "reference-reply")["post"]["id"], "reference-reply")
            self.assertEqual(self.runtime.history(actor, "private-channel")["posts"][0]["id"], "private-root")
            page = self.runtime.threads(actor, "reference-dm")
            self.assertIsNone(page["threads"][0]["unread_count"])
            self.assertEqual(self.runtime.download(actor, "reference-reply", "image", "explicit")["file_id"], "image")
            self.assertEqual(self.state(), before)
            self.assertEqual(self.backend.change_calls, feed_calls)
            with self.assertRaisesRegex(ValueError, "tracked inbox"):
                self.runtime.read(actor, "reference-dm")
            with self.assertRaisesRegex(ValueError, "tracked accessible unread"):
                self.runtime.prepare_read(actor, post_id="reference-reply")
            self.assertEqual(self.state(), before)

    def test_own_reference_stars_are_zero_unread_quiet_and_not_other_agents_saved_work(self):
        before = self.state()
        self.runtime.set_saved(self.actor, "reference-reply", True)
        self.assertEqual(self.state(), before)
        own = {c["id"]: c for c in self.runtime.inbox(self.actor)}["reference-dm"]
        self.assertEqual((own["role"], own["muted"], own["unread_count"], own["starred_count"]), (None, True, 0, 1))
        self.assertEqual(own["starred"][0]["post_id"], "reference-reply")
        self.assertNotIn("reference-dm", {c["id"] for c in self.runtime.inbox(self.manual.key)})
        self.assertNotIn("reference-dm", {c["id"] for c in self.ledger.conversations(self.actor)})
        self.assertEqual(self.state(), before)
        self.runtime.set_saved(self.actor, "reference-reply", False)
        self.assertNotIn("reference-dm", {c["id"] for c in self.runtime.inbox(self.actor)})
        self.assertEqual(self.state(), before)

    def test_disabled_policy_denies_new_references_but_preserves_legacy_access(self):
        self.runtime.set_saved(self.actor, "reference-reply", True)
        self.backend.enabled = False
        before = self.state()
        for action in (lambda: self.runtime.conversation_directory(self.actor),
                       lambda: self.runtime.show(self.actor, "reference-reply"),
                       lambda: self.runtime.history(self.actor, "reference-dm"),
                       lambda: self.runtime.threads(self.actor, "private-channel"),
                       lambda: self.runtime.download(self.actor, "reference-reply", "image", "explicit")):
            with self.assertRaises(BackendError) as error:
                action()
            self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.runtime.show(self.actor, "tracked")["id"], "tracked")
        unavailable = next(c for c in self.runtime.inbox(self.actor) if c["id"] is None)
        self.assertEqual(unavailable["starred"], [dict(post_id="reference-reply", unavailable=True)])
        self.runtime.set_saved(self.actor, "reference-reply", False)
        self.assertEqual(self.state(), before)

    def test_unenrolled_and_bad_selectors_fail_before_directory_calls(self):
        before = self.state()
        with self.assertRaises(LedgerError):
            self.runtime.conversation_directory("codex:unknown")
        for options in (dict(limit=0), dict(limit=101), dict(limit=True), dict(query=None),
                        dict(kind="group"), dict(agent="Reviewer")):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.runtime.conversation_directory(self.actor, **options)
        self.assertEqual(self.backend.directory_calls, [])
        self.assertEqual(self.state(), before)

    def test_malformed_descriptors_and_cross_conversation_content_are_rejected(self):
        normal = deepcopy(self.backend.directory_page)
        for changes in (dict(open=True), dict(visibility="public"), dict(role="owner")):
            self.backend.directory_page = deepcopy(normal)
            self.backend.directory_page["conversations"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.runtime.conversation_directory(self.actor)
        self.backend.directory_page = deepcopy(normal)
        self.backend.directory_page["conversations"][0]["participants"][0]["kind"] = "email"
        with self.assertRaisesRegex(RuntimeError, "participants"):
            self.runtime.conversation_directory(self.actor)
        self.backend.posts = lambda *_args: {"posts": [self.backend.canonical["tracked"]]}
        with self.assertRaisesRegex(RuntimeError, "another conversation"):
            self.runtime.history(self.actor, "reference-dm")
        with self.assertRaisesRegex(RuntimeError, "another conversation"):
            self.runtime.history(self.actor, "private-channel", "reference-root")
        for chat in normal["conversations"]:
            self.assertEqual(conversation_descriptor(chat, chat["id"]), chat)


class ManagedReadWireTests(unittest.TestCase):
    def test_content_and_own_saved_opt_in_without_changing_attention_requests(self):
        calls = []
        backend = Backend("http://fixture.invalid", "fixture")
        backend.request = lambda *args, **query: calls.append((args, query)) or {"post": {}, "saved": []}
        backend.posts("actor", "room", 2, "before")
        backend.post("actor", "post")
        backend.root("actor", "root")
        backend.thread_page("actor", "root", 2, "before")
        backend.threads("actor", "room", 2, "before")
        backend.saved("actor")
        backend.set_saved("actor", "post", True)
        backend.conversation_directory("actor", "work", "dm", "claude:peer", 2, "opaque")
        self.assertTrue(all(query.get("read_version") == 1 for _, query in calls))
        self.assertEqual(calls[6][0][2], dict(actor="actor", starred=True))
        self.assertEqual(calls[7], (("GET", "/conversations/directory"), dict(actor="actor", query="work",
                         kind="dm", agent="claude:peer", limit=2, after="opaque", read_version=1)))
        calls.clear()
        backend.changes("actor", 7, 2)
        self.assertEqual(calls, [(("GET", "/changes"), dict(actor="actor", cursor=7,
                          generation=2, limit=100, attention_version=3))])

    def test_attachment_metadata_and_bytes_use_the_same_explicit_read_contract(self):
        backend = Backend("http://fixture.invalid", "fixture")
        calls, body = [], b"abc"

        def response(method, path, data=None, **query):
            calls.append((method, path, query))
            value = io.BytesIO(json.dumps({"file": {"id": "image", "size": len(body)}}).encode()
                               if path.endswith("/info") else body)
            value.headers = {"Content-Length": str(len(body)), "X-Content-SHA256": hashlib.sha256(body).hexdigest()}
            return value

        backend._open_attachment = response
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "chosen.png"
            self.assertEqual(backend.download("actor", "post", "image", output)["size"], len(body))
            self.assertEqual(output.read_bytes(), body)
        self.assertEqual(calls, [("GET", "/posts/post/files/image/info", dict(actor="actor", read_version=1)),
                                 ("GET", "/posts/post/files/image", dict(actor="actor", read_version=1))])
