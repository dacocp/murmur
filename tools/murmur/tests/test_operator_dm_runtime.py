"""Opt-in real human-DM adoption with isolated receipts and fake native delivery."""

import json
import os
from pathlib import Path
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

import test_mattermost_runtime as fixtures
from server.fixture import load_fixture
from murmur.backend import BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger


@unittest.skipUnless(os.environ.get("MURMUR_TEST_STATE"), "marked Mattermost fixture not selected (MURMUR_TEST_STATE)")
class OperatorDMRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = load_fixture(require_admin=True)
        self.origin = fixture.origin
        self.admin = fixture.admin
        fixtures.MattermostRuntimeTests.setUp(self)

    tearDown = fixtures.MattermostRuntimeTests.tearDown
    identity = fixtures.MattermostRuntimeTests.identity
    event = fixtures.MattermostRuntimeTests.event

    def call(self, path, data=None, expected=200):
        request = Request(self.origin + path, method="GET" if data is None else "POST",
                          data=None if data is None else json.dumps(data).encode(), headers={
                              "Authorization": "Bearer " + self.admin["token"],
                              "X-Murmur-User-Id": self.admin["user_id"], "Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.status, expected, path)
            return json.load(response)

    def open_dm(self, actor, operation=None, expected=200):
        return self.call("/plugins/murmur/operator/v1/dms",
                         {"agent_id": actor, "op_id": operation or str(uuid.uuid4())}, expected)

    def attention(self, actor):
        return {table: [tuple(row) for row in self.ledger.db.execute(
            "SELECT * FROM " + table + " WHERE actor=?", (actor,))]
                for table in ("receipts", "attempts", "native_turns", "outbound")}

    def test_existing_native_history_is_quiet_and_first_human_request_keeps_its_own_attention(self):
        parent, child, grandchild = self.a, self.identity(self.a), None
        self.runtime.register(child, "Murmur human-DM child fixture")
        grandchild = self.identity(child)
        self.runtime.register(grandchild, "Murmur human-DM grandchild fixture")
        bot_id = self.ledger.actor(parent.key)["user_id"]
        native = self.call("/api/v4/channels/direct", [self.admin["user_id"], bot_id], expected=201)
        channel = native["id"]
        at = int(time.time() * 1000)
        history = []
        for i in range(32):
            history.append(self.call("/api/v4/posts?set_online=false", {
                "channel_id": channel, "message": "Historical human request " + str(i),
                "create_at": at, "props": {"notification_eligible": True}}, expected=201))
        self.assertTrue(all(p["create_at"] >= self.ledger.actor(parent.key)["enrolled_at"] for p in history))
        self.runtime.sync(parent.key)
        self.assertEqual(self.ledger.conversations(parent.key), [])
        operation = str(uuid.uuid4())
        opened = self.open_dm(parent.key, operation)["conversation"]
        self.assertEqual((opened["id"], opened["kind"]), (channel, "dm"))
        self.assertEqual(self.open_dm(parent.key, operation)["conversation"], opened)
        for actor in (parent.key, child.key, grandchild.key):
            self.runtime.sync(actor)
            chat = next(c for c in self.ledger.conversations(actor) if c["id"] == channel)
            self.assertEqual(chat["role"], "member" if actor == parent.key else "observer")
            self.assertEqual(bool(chat["muted"]), actor != parent.key)
            unread = self.ledger._unread(actor, channel_id=channel)
            self.assertEqual(len(unread), 32 if actor == parent.key else 0)
            self.assertTrue(all(p["announced_at"] is None and not p["notification_eligible"] for p in unread))
        self.assertIsNone(self.runtime.deliver(parent))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(len(self.backend.posts(parent.key, channel, 100)["posts"]), 32)
        self.runtime.set_saved(parent.key, history[0]["id"], True)
        selected_history = self.runtime.read(parent.key, channel, root=history[0]["id"], limit=1)
        self.assertEqual([p["id"] for p in selected_history], [history[0]["id"]])
        self.ledger.mark_read(parent.key, [history[0]["id"]])
        before = self.attention(parent.key)
        self.assertEqual(self.open_dm(parent.key)["conversation"]["id"], channel)
        self.runtime.sync(parent.key)
        self.assertEqual(self.attention(parent.key), before)
        self.assertEqual([p["post_id"] for p in self.backend.saved(parent.key)], [history[0]["id"]])

        request = self.call("/plugins/murmur/operator/v1/posts", {
            "channel_id": channel, "message": "Fresh request: please review this", "op_id": str(uuid.uuid4())})["post"]
        self.assertEqual((request["user_id"], request["root_id"]), (self.admin["user_id"], ""))
        self.runtime.sync(parent.key)
        inbox = next(c for c in self.runtime.inbox(parent.key) if c["id"] == channel)
        self.assertEqual((inbox["latest_post_id"], inbox["unread_count"], inbox["starred_count"]),
                         (request["id"], 32, 1))
        self.runtime.deliver(parent)
        attempt = self.ledger.pending_attempts(parent.key)[0]
        self.assertEqual(attempt["post_ids"], [request["id"]])
        self.event(parent, "submit", "human-request-turn", attempt["token"])
        selected = self.runtime.read(parent.key, channel, root=request["id"], limit=20)
        self.assertEqual([p["id"] for p in selected], [request["id"]])
        self.ledger.mark_read(parent.key, [request["id"]])
        reply = self.runtime.reply(parent.key, request["id"], "Reviewed in the exact thread")["post"]
        self.assertEqual(reply["root_id"], request["id"])
        self.assertEqual(reply["user_id"], bot_id)
        self.assertEqual(reply["causal"]["chain_ids"], ["origin:" + request["id"]])
        self.event(parent, "complete", "human-request-turn")
        self.assertIsNone(self.runtime.deliver(parent))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.ledger._unread(parent.key, channel_id=channel)), 31)
        self.assertTrue(all(p["announced_at"] is None for p in self.ledger._unread(parent.key, channel_id=channel)))
        with self.assertRaises(BackendError) as denied:
            self.runtime.reply(child.key, request["id"], "Observer must not write")
        self.assertEqual(denied.exception.status, 403)
        # The old completed open must never replay grants after departure.
        self.runtime.mutate(parent.key, "leave", channel_id=channel)
        self.assertEqual(self.open_dm(parent.key, operation)["conversation"]["id"], channel)
        self.open_dm(parent.key, expected=409)
        self.assertEqual(self.backend.post(grandchild.key, history[0]["id"])["id"], history[0]["id"])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        self.runtime.sync(grandchild.key)
        self.assertEqual(self.ledger._unread(grandchild.key, channel_id=channel), [])
        self.assertEqual(len(self.backend.posts(grandchild.key, channel, 100)["posts"]), 34)

    def test_new_manual_pair_opens_empty_and_saves_work_without_native_delivery(self):
        manual = self.identity()
        self.runtime.register(manual, "Murmur human-DM manual fixture", mode="manual")
        opened = self.open_dm(manual.key)["conversation"]
        self.assertEqual(self.backend.posts(manual.key, opened["id"])["posts"], [])
        request = self.call("/plugins/murmur/operator/v1/posts", {
            "channel_id": opened["id"], "message": "Manual follow-up", "op_id": str(uuid.uuid4())})["post"]
        self.runtime.sync(manual.key)
        self.assertEqual(self.runtime.inbox(manual.key)[0]["unread_count"], 1)
        self.runtime.set_saved(manual.key, request["id"], True)
        self.assertIsNone(self.runtime.deliver(manual))
        self.assertEqual(self.ledger.attempts(manual.key), [])
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
