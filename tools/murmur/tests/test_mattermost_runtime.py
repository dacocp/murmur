"""Opt-in real plugin + fresh ledger + fake native transport integration.

Run with MURMUR_TEST_STATE pointing to a marked disposable fixture. Uses
explicitly named synthetic identities and fake native transport; no live fallback.
"""

import json
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.hooks import handle
from murmur.ledger import Ledger
from murmur.native import DeliveryResult, HookEvent, NativeIdentity
from server.fixture import load_fixture


class FakeNative:
    def __init__(self):
        self.calls = []

    def deliver(self, identity, message):
        self.calls.append((identity, message))
        return DeliveryResult("accepted", "fixture transport only", identity.target)


@unittest.skipUnless(os.environ.get("MURMUR_TEST_STATE"), "marked Mattermost fixture not requested")
class MattermostRuntimeTests(unittest.TestCase):
    def setUp(self):
        settings = load_fixture().coordinator
        self.backend = Backend(settings.get("server_url", settings.get("url")), settings["token"])
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path)
        self.transport = FakeNative()
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        self.a = self.identity()
        self.b = self.identity()
        self.runtime.register(self.a, "Murmur runtime A")
        self.runtime.register(self.b, "Murmur runtime B")

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def identity(self, parent=None):
        return NativeIdentity("codex", str(uuid.uuid4()), parent.session_id if parent else None,
                              True, {"native_epoch": "fixture"}, {"fixture": True})

    def event(self, identity, kind, key, token=None):
        prompt = self.transport.calls[-1][1] if token else "ordinary test input"
        captured = HookEvent(identity, kind, "UserPromptSubmit" if kind == "submit" else "Stop",
                             key, prompt, token)
        with patch("murmur.native.parse_hook", return_value=captured):
            return handle(self.ledger, "codex", captured.native_event, {"prompt": prompt})

    def send(self, identity, channel, message, root=""):
        return self.runtime.mutate(identity.key, "post", channel_id=channel,
                                   message=message, root_id=root)["post"]

    def test_threads_inheritance_native_admission_selective_read_reply_and_restart(self):
        channel = self.runtime.mutate(self.a.key, "conversation", kind="channel", name="Build review",
                                    members=[self.b.key])["conversation"]["id"]
        dm = self.runtime.mutate(self.a.key, "conversation", kind="dm", members=[self.b.key])["conversation"]["id"]
        original = self.send(self.a, channel, "Build is ready for review")
        child = self.identity(self.a)
        grandchild = self.identity(child)
        self.runtime.register(child, "Murmur runtime child")
        self.runtime.register(grandchild, "Murmur runtime grandchild")
        for actor in (child.key, grandchild.key):
            self.runtime.sync(actor)
            self.assertTrue(all(c["role"] == "observer" and c["muted"]
                                for c in self.ledger.conversations(actor)))
            self.assertEqual(self.backend.post(actor, original["id"])["message"], original["message"])
            with self.assertRaises(BackendError) as denied:
                self.runtime.mutate(actor, "post", channel_id=dm, message="not permitted")
            self.assertEqual(denied.exception.status, 403)
        self.runtime.mutate(child.key, "join", channel_id=channel)
        own_dm = self.runtime.mutate(child.key, "conversation", kind="dm", members=[self.b.key])["conversation"]["id"]
        self.assertNotEqual(own_dm, dm)
        self.assertEqual(self.backend.posts(child.key, own_dm)["posts"], [])
        self.runtime.sync(self.b.key)
        self.runtime.deliver(self.b)
        attempt = self.ledger.pending_attempts(self.b.key)[0]
        self.assertEqual(attempt["transport"], "accepted")
        self.assertEqual(sum(p["announced_at"] is not None for p in self.ledger.unread_posts(self.b.key)), 0)
        self.assertEqual(self.event(self.b, "submit", "turn-one", attempt["token"]), {})
        inbox = self.runtime.inbox(self.b.key)
        self.assertEqual(inbox[0]["preview"], original["message"])
        self.assertEqual(inbox[0]["unread_count"], 1)
        self.assertEqual(len(self.runtime.history(self.b.key, channel)["posts"]), 1)
        self.event(self.b, "complete", "turn-one")
        self.assertIsNone(self.runtime.deliver(self.b))  # ignored, announced backlog
        later = self.send(self.a, channel, "Please also check the API contract")
        self.runtime.sync(self.b.key)
        self.runtime.deliver(self.b)
        second = self.ledger.pending_attempts(self.b.key)[0]
        self.assertEqual(second["post_ids"], [later["id"]])
        self.event(self.b, "submit", "turn-two", second["token"])
        returned = self.runtime.read(self.b.key, channel, limit=1)
        concurrent = self.send(self.a, channel, "A later concurrent detail")
        self.runtime.sync(self.b.key)
        self.ledger.mark_read(self.b.key, [p["id"] for p in returned])
        self.assertEqual({p["id"] for p in self.ledger.unread_posts(self.b.key)}, {later["id"], concurrent["id"]})
        reply = self.send(self.b, channel, "Reviewed; one API question", later["id"])
        self.assertEqual(reply["root_id"], later["id"])
        self.assertTrue(reply["causal"]["chain_ids"])
        self.assertEqual([p["id"] for p in self.backend.thread(self.a.key, later["id"])],
                         [later["id"], reply["id"]])
        self.event(self.b, "complete", "stale-turn")
        self.assertEqual(self.ledger.active_turn(self.b.key)["turn_key"], "turn-two")
        self.event(self.b, "complete", "turn-two")
        # A parent's departure preserves grants, and later parent chats propagate.
        self.runtime.mutate(self.a.key, "leave", channel_id=channel)
        self.assertEqual(self.backend.post(grandchild.key, original["id"])["id"], original["id"])
        new_group = self.runtime.mutate(self.a.key, "conversation", kind="channel", name="Later API work",
                                        members=[self.b.key])["conversation"]["id"]
        self.assertIn(new_group, {c["id"] for c in self.backend.conversations(grandchild.key)})
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        self.runtime.sync(self.b.key)
        self.assertNotIn(original["id"], {p["id"] for p in self.ledger.unread_posts(self.b.key)})
        self.assertEqual(self.ledger.db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_registration_rename_preserves_identity_saved_work_and_attention(self):
        channel = self.runtime.mutate(self.a.key, "conversation", kind="channel", name="Rename review",
                                    members=[self.b.key])["conversation"]["id"]
        root = self.send(self.b, channel, "Keep this request after a session rename")
        self.runtime.sync(self.a.key)
        self.runtime.set_saved(self.a.key, root["id"], True)
        # Conversation creation advances access generation. Capture the current
        # canonical registration immediately before renaming, not initial setup.
        original = self.runtime.register(self.a, "Murmur runtime A")
        self.runtime.deliver(self.a)

        def attention():
            return {table: [tuple(row) for row in self.ledger.db.execute("SELECT * FROM " + table)]
                    for table in ("conversations", "receipts", "attempts", "native_turns", "outbound")}

        before = attention()
        for _ in range(2):
            renamed = self.runtime.register(self.a, "Exact renamed native session title")
            self.assertEqual(renamed["name"], "Exact renamed native session title")
            for field in ("id", "user_id", "parent", "registered_at", "generation"):
                self.assertEqual(renamed.get(field), original.get(field))
            self.assertEqual(self.ledger.actor(self.a.key)["name"], renamed["name"])
            self.assertEqual(attention(), before)
            self.assertEqual([p["post_id"] for p in self.backend.saved(self.a.key)], [root["id"]])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.runtime.inbox(self.a.key)[0]["unread_count"], 1)

    def test_bounded_navigation_exact_replies_and_invalid_selectors_against_real_server(self):
        label = "Murmur navigation " + uuid.uuid4().hex[:10]
        self.runtime.register(self.a, label)
        self.runtime.register(self.b, label)
        first = self.runtime.agents(query=label, limit=1)
        second = self.runtime.agents(query=label, limit=1, after=first["next_after"])
        self.assertEqual({first["agents"][0]["id"], second["agents"][0]["id"]},
                         {self.a.key, self.b.key})
        self.assertIsNone(second["next_after"])
        self.assertEqual(set(first["agents"][0]), {"id", "name", "username"})
        self.assertRegex(first["agents"][0]["username"], r"^[a-z][a-z0-9-]*-[a-f0-9]{8}$")
        channel = self.runtime.mutate(self.a.key, "conversation", kind="channel", name=label,
                                    members=[self.b.key])["conversation"]["id"]
        root = self.send(self.a, channel, "A request " + "λ" * 200)
        replies = [self.send(self.a, channel, "Detail " + str(i), root["id"]) for i in range(4)]
        other = self.send(self.a, channel, "Independent request")
        self.runtime.sync(self.b.key)
        self.runtime.set_saved(self.b.key, replies[0]["id"], True)

        def attention():
            return {table: [tuple(row) for row in self.ledger.db.execute("SELECT * FROM " + table)]
                    for table in ("receipts", "attempts", "native_turns", "outbound")}

        before = attention()
        summaries = self.runtime.threads(self.b.key, channel, limit=1)
        older = self.runtime.threads(self.b.key, channel, limit=1, before=summaries["next_before"])
        by_id = {s["id"]: s for s in summaries["threads"] + older["threads"]}
        self.assertEqual(set(by_id), {root["id"], other["id"]})
        self.assertEqual(by_id[root["id"]]["unread_count"], 5)
        self.assertEqual(by_id[root["id"]]["reply_count"], 4)
        self.assertLessEqual(len(by_id[root["id"]]["preview"]), 160)
        self.assertNotIn("message", by_id[root["id"]])
        page = self.runtime.history(self.b.key, channel, root["id"], limit=2)
        earlier = self.runtime.history(self.b.key, channel, root["id"], limit=2, before=page["next_before"])
        self.assertEqual(page["root"]["id"], root["id"])
        ordered = sorted([root, *replies], key=lambda p: (p["create_at"], p["id"]))
        self.assertEqual((len(page["posts"]), len(earlier["posts"])), (2, 2))
        self.assertEqual([p["id"] for p in earlier["posts"] + page["posts"]],
                         [p["id"] for p in ordered[-4:]])
        context = self.runtime.context(self.b.key, replies[0]["id"], limit=1)
        self.assertEqual(context["post"]["id"], replies[0]["id"])
        self.assertEqual(context["context"]["root"]["id"], root["id"])
        self.assertEqual(len(context["context"]["posts"]), 1)
        for bad_root in (replies[0]["id"], "z" * 26):
            with self.assertRaises(BackendError):
                self.runtime.read(self.b.key, channel, root=bad_root)
        with self.assertRaises(ValueError):
            self.runtime.read(self.b.key, "z" * 26)
        with self.assertRaises(BackendError):
            self.runtime.history(self.b.key, channel, root["id"], before=other["id"])
        self.assertEqual(attention(), before)
        self.assertEqual([s["post_id"] for s in self.backend.saved(self.b.key)], [replies[0]["id"]])
        literal = 'Reviewed λ\n`code` "$literal"\n$(not a shell)\n'
        reply = self.runtime.reply(self.b.key, replies[0]["id"], literal)["post"]
        self.assertEqual((reply["channel_id"], reply["root_id"], reply["message"]),
                         (channel, root["id"], literal))
        self.assertEqual(reply["user_id"], self.ledger.actor(self.b.key)["user_id"])
        self.assertEqual(self.ledger.outbounds(self.b.key)[0]["state"], "complete")
        self.assertEqual(attention()["receipts"], before["receipts"])
        self.assertEqual(self.transport.calls, [])

    def test_manual_sidechat_has_no_inheritance_and_can_read_reply_and_save_without_wakes(self):
        hidden = self.runtime.mutate(self.a.key, "conversation", kind="dm",
                                     members=[self.b.key])["conversation"]["id"]
        hidden_post = self.send(self.a, hidden, "No inherited access in manual mode")
        sidechat = NativeIdentity("codex", str(uuid.uuid4()))
        registered = self.runtime.register(sidechat, mode="manual")
        self.assertNotEqual(registered["user_id"], self.ledger.actor(self.a.key)["user_id"])
        self.assertEqual(self.backend.conversations(sidechat.key), [])
        with self.assertRaises(BackendError) as denied:
            self.runtime.show(sidechat.key, hidden_post["id"])
        self.assertEqual(denied.exception.status, 403)
        public = self.runtime.mutate(self.a.key, "conversation", kind="channel", name="Public work",
                                     members=[self.b.key])["conversation"]["id"]
        visible = self.send(self.a, public, "Public preview is not inherited membership")
        self.assertEqual(self.runtime.show(sidechat.key, visible["id"])["id"], visible["id"])
        self.assertEqual(self.runtime.inbox(sidechat.key), [])
        self.assertEqual(self.backend.conversations(sidechat.key), [])
        # Later proof remains metadata; the explicitly independent account never
        # acquires either old or newly created ancestor conversations.
        observed_parent = replace(sidechat, parent_known=True, parent_session_id=self.a.session_id)
        self.runtime.register(observed_parent, mode="manual")
        self.runtime.mutate(self.a.key, "conversation", kind="channel", name="Later parent only",
                            members=[self.b.key])
        self.assertEqual(self.backend.conversations(sidechat.key), [])
        dm = self.runtime.mutate(sidechat.key, "conversation", kind="dm", members=[self.b.key])["conversation"]["id"]
        channel = self.runtime.mutate(sidechat.key, "conversation", kind="channel", name="Sidechat work",
                                   members=[self.b.key])["conversation"]["id"]
        request = self.send(self.b, dm, "Please review this when prompted")
        self.send(self.b, channel, "A separate channel question")
        inbox = self.runtime.inbox(sidechat.key)
        self.assertEqual({c["id"] for c in inbox}, {dm, channel})
        self.assertIsNone(self.runtime.deliver(sidechat))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ledger.attempts(sidechat.key), [])
        self.assertTrue(all(p["announced_at"] is None for p in self.ledger.unread_posts(sidechat.key)))
        returned = self.runtime.read(sidechat.key, dm, limit=1)
        self.assertEqual([p["id"] for p in returned], [request["id"]])
        self.ledger.mark_read(sidechat.key, [request["id"]])
        self.runtime.set_saved(sidechat.key, request["id"], True)
        reply = self.send(sidechat, dm, "Reviewed manually", request["id"])
        self.assertEqual(reply["user_id"], registered["user_id"])
        self.assertEqual([p["id"] for p in self.backend.thread(sidechat.key, request["id"])],
                         [request["id"], reply["id"]])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        renamed = self.runtime.register(observed_parent, "Explicit sidechat label", mode="manual")
        self.assertEqual(renamed["user_id"], registered["user_id"])
        self.assertEqual(self.ledger.actor(sidechat.key)["mode"], "manual")
        saved = self.runtime.inbox(sidechat.key, starred_only=True)
        self.assertEqual([(c["id"], c["unread_count"], c["starred_count"]) for c in saved], [(dm, 0, 1)])
        self.assertEqual(self.ledger.attempts(sidechat.key), [])

    def test_native_saved_posts_keep_read_work_visible_without_attention_changes(self):
        channel = self.runtime.mutate(self.a.key, "conversation", kind="channel", name="Saved review",
                                    members=[self.b.key])["conversation"]["id"]
        root = self.send(self.a, channel, "Review this when you have time")
        self.runtime.sync(self.b.key)
        # A queued but unadmitted arrival remains a separate fact from saving.
        self.runtime.deliver(self.b)
        returned = self.runtime.read(self.b.key, channel)
        self.ledger.mark_read(self.b.key, [p["id"] for p in returned])
        self.assertEqual(self.runtime.inbox(self.b.key), [])

        def attention():
            return {table: [tuple(row) for row in self.ledger.db.execute("SELECT * FROM " + table)]
                    for table in ("receipts", "attempts", "native_turns", "outbound")}

        before = attention()
        for _ in range(2):
            self.assertEqual(self.runtime.set_saved(self.b.key, root["id"], True),
                             {"post_id": root["id"], "starred": True})
        inbox = self.runtime.inbox(self.b.key)
        self.assertEqual((inbox[0]["unread_count"], inbox[0]["starred_count"]), (0, 1))
        self.assertEqual(inbox[0]["starred"][0]["post_id"], root["id"])
        self.assertEqual(self.runtime.show(self.b.key, root["id"]), root)
        self.assertEqual(attention(), before)
        self.assertEqual(len(self.transport.calls), 1)

        own = self.send(self.b, channel, "My follow-up in the original discussion", root["id"])
        self.runtime.sync(self.b.key)
        self.runtime.set_saved(self.b.key, own["id"], True)
        saved = self.runtime.inbox(self.b.key, starred_only=True)[0]
        self.assertEqual(saved["unread_count"], 0)
        self.assertEqual({p["post_id"] for p in saved["starred"]}, {root["id"], own["id"]})
        self.assertEqual(next(p for p in saved["starred"] if p["post_id"] == own["id"])["root_id"], root["id"])
        self.assertFalse(self.ledger.db.execute(
            "SELECT 1 FROM receipts WHERE actor=? AND post_id=?", (self.b.key, own["id"])).fetchone())

        self.runtime.set_saved(self.a.key, root["id"], True)
        child = self.identity(self.a)
        self.runtime.register(child, "Murmur saved observer")
        self.assertEqual(self.backend.saved(child.key), [])
        self.runtime.set_saved(child.key, own["id"], True)
        observer = self.runtime.inbox(child.key, starred_only=True)[0]
        self.assertEqual(observer["role"], "observer")
        self.assertTrue(observer["muted"])
        self.assertEqual(observer["starred"][0]["post_id"], own["id"])

        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        self.assertEqual(self.runtime.inbox(self.b.key, starred_only=True)[0]["starred_count"], 2)
        for _ in range(2):
            self.runtime.set_saved(self.b.key, root["id"], False)
        self.assertEqual(self.runtime.inbox(self.b.key, starred_only=True)[0]["starred_count"], 1)
        self.runtime.set_saved(self.b.key, own["id"], False)
        self.assertEqual(self.runtime.inbox(self.b.key), [])
        self.assertEqual([p["post_id"] for p in self.backend.saved(child.key)], [own["id"]])
        self.assertEqual([p["post_id"] for p in self.backend.saved(self.a.key)], [root["id"]])
        self.assertEqual(len(self.transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
