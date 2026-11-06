"""Native human-DM discovery on a marked server; isolated ledger/fake delivery."""

from contextlib import contextmanager
import json
import os
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import test_mattermost_runtime as fixtures
from server.fixture import load_fixture
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError


@unittest.skipUnless(os.environ.get("MURMUR_TEST_STATE"), "marked Mattermost fixture not selected (MURMUR_TEST_STATE)")
class OperatorDMRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = load_fixture(require_admin=True)
        self.origin = fixture.origin
        self.admin = fixture.admin
        self.coordinator_token = fixture.coordinator["token"]
        self.original_plugin = self.plugin_settings()
        self.addCleanup(self.restore_plugin, self.original_plugin)
        # Configure the fixture before enrollment so successful registration
        # proves the native human/bot pair did not exist. Expanded read access
        # is deliberately disabled: this is normal participating DM attention.
        self.configure_plugin(operatoruserid=self.admin["user_id"], allowmanagedconversationreads=False)
        fixtures.MattermostRuntimeTests.setUp(self)

    tearDown = fixtures.MattermostRuntimeTests.tearDown
    identity = fixtures.MattermostRuntimeTests.identity
    event = fixtures.MattermostRuntimeTests.event

    def call(self, path, data=None, expected=200, *, method=None):
        request = Request(self.origin + path, method=method or ("GET" if data is None else "POST"),
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

    def plugin_settings(self):
        return self.call("/api/v4/config")["PluginSettings"]["Plugins"].get("murmur", {})

    def restore_plugin(self, settings):
        config = self.call("/api/v4/config")
        # Mattermost masks secrets on GET. Keep the actual credential from the
        # protected disposable marker while restoring every other plugin key.
        config["PluginSettings"]["Plugins"]["murmur"] = dict(settings, coordinatortoken=self.coordinator_token)
        self.call("/api/v4/config", config, method="PUT")

    def configure_plugin(self, **changes):
        self.restore_plugin(dict(self.plugin_settings(), **changes))

    @contextmanager
    def operator_unconfigured(self):
        settings = self.plugin_settings()
        self.configure_plugin(operatoruserid="")
        try:
            yield
        finally:
            self.restore_plugin(settings)

    def native_dm(self, actor):
        bot = self.ledger.actor(actor)["user_id"]
        return self.call("/api/v4/channels/direct", [self.admin["user_id"], bot], expected=201)["id"]

    def native_pair_channels(self, actor):
        bot = self.ledger.actor(actor)["user_id"]
        name = "__".join(sorted([self.admin["user_id"], bot]))
        return [c["id"] for c in self.call("/api/v4/users/" + bot + "/channels")
                if c["type"] == "D" and c["name"] == name]

    def native_post(self, channel, message, *, root="", **values):
        return self.call("/api/v4/posts?set_online=false", dict(
            channel_id=channel, message=message, root_id=root, **values), expected=201)

    def sync_until(self, actor, post_ids):
        deadline = time.monotonic() + 15
        while True:
            self.runtime.sync(actor)
            unread = self.ledger._unread(actor)
            if set(post_ids) <= {post["id"] for post in unread}:
                return unread
            if time.monotonic() >= deadline:
                self.fail("Native human DM posts did not reach the recipient's ordinary feed")
            time.sleep(.1)

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)

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
        # Explicit adoption wins while automatic contact is unconfigured. Its
        # exact canonical baseline remains quiet, even with misleading props.
        with self.operator_unconfigured():
            channel = self.native_dm(parent.key)
            at = int(time.time() * 1000)
            history = [self.native_post(channel, "Historical human request " + str(i),
                create_at=at, props={"notification_eligible": True}) for i in range(32)]
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
        with self.assertRaisesRegex(LedgerError, "full participation"):
            self.runtime.reply(child.key, request["id"], "Observer must not write")
        # The old completed open must never replay grants after departure.
        self.runtime.mutate(parent.key, "leave", channel_id=channel)
        self.assertEqual(self.open_dm(parent.key, operation)["conversation"]["id"], channel)
        self.open_dm(parent.key, expected=409)
        self.native_post(channel, "A native human post must not rejoin a departed agent")
        self.runtime.sync(parent.key)
        self.assertNotIn(channel, {c["id"] for c in self.ledger.conversations(parent.key)})
        self.assertEqual(self.backend.post(grandchild.key, history[0]["id"])["id"], history[0]["id"])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        self.runtime.sync(grandchild.key)
        self.assertEqual(self.ledger._unread(grandchild.key, channel_id=channel), [])
        self.assertEqual(len(self.backend.posts(grandchild.key, channel, 100)["posts"]), 35)

    def test_first_native_human_post_discovers_dm_and_delivers_once(self):
        parent, child = self.a, self.identity(self.a)
        self.runtime.register(child, "Native human-DM observer fixture")
        self.assertEqual(self.native_pair_channels(parent.key), [], "Enrollment must not create an empty native DM")
        channel = self.native_dm(parent.key)
        request = self.native_post(channel, "First native human request; no Murmur open command")
        unread = self.sync_until(parent.key, [request["id"]])
        self.assertEqual([(p["id"], bool(p["notification_eligible"]), p["announced_at"]) for p in unread],
                         [(request["id"], True, None)])
        self.assertEqual(self.ledger.outbounds(parent.key), [])
        self.assertFalse(self.backend.request("GET", "/health")["capabilities"]["managed_conversation_reads"]["enabled"])
        chat = next(c for c in self.ledger.conversations(parent.key) if c["id"] == channel)
        self.assertEqual((chat["kind"], chat["role"], chat["notification_mode"]), ("dm", "member", "all"))
        self.runtime.sync(child.key)
        inherited = next(c for c in self.ledger.conversations(child.key) if c["id"] == channel)
        self.assertEqual((inherited["role"], inherited["notification_mode"]), ("observer", "off"))
        self.assertEqual(self.ledger._unread(child.key), [])
        self.assertEqual(self.backend.post(child.key, request["id"])["id"], request["id"])
        self.runtime.deliver(parent)
        attempt = self.ledger.pending_attempts(parent.key)[0]
        self.assertEqual(attempt["post_ids"], [request["id"]])
        self.event(parent, "submit", "native-human-first-turn", attempt["token"])
        selected = self.runtime.read(parent.key, channel, root=request["id"], limit=1)
        self.assertEqual([p["id"] for p in selected], [request["id"]])
        self.ledger.mark_read(parent.key, [request["id"]])
        reply = self.runtime.reply(parent.key, request["id"], "Received the first native human request")["post"]
        self.assertEqual(reply["root_id"], request["id"])
        self.event(parent, "complete", "native-human-first-turn")
        self.reopen()
        self.runtime.sync(parent.key)
        self.assertIsNone(self.runtime.deliver(parent))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.native_dm(parent.key), channel)
        self.assertEqual(self.native_pair_channels(parent.key), [channel])
        members = self.call("/api/v4/channels/" + channel + "/members")
        self.assertEqual({m["user_id"] for m in members},
                         {self.admin["user_id"], self.ledger.actor(parent.key)["user_id"]})
        self.assertEqual([c["id"] for c in self.ledger.conversations(parent.key) if c["kind"] == "dm"], [channel])

    def test_first_native_post_to_manual_recipient_is_visible_without_delivery(self):
        manual = self.identity()
        self.runtime.register(manual, "Native human-DM manual fixture", mode="manual")
        channel = self.native_dm(manual.key)
        request = self.native_post(channel, "Manual inbox should contain the first native request")
        self.sync_until(manual.key, [request["id"]])
        self.assertEqual(self.runtime.inbox(manual.key)[0]["unread_count"], 1)
        self.runtime.set_saved(manual.key, request["id"], True)
        self.assertIsNone(self.runtime.deliver(manual))
        self.assertEqual(self.ledger.attempts(manual.key), [])
        self.assertEqual(self.ledger.outbounds(manual.key), [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.native_dm(manual.key), channel)

    def test_absent_pair_missed_callback_recovers_after_plugin_and_ledger_restart_once(self):
        parent = self.a
        self.call("/api/v4/plugins/murmur/disable", {})
        try:
            channel = self.native_dm(parent.key)
            # A backdated canonical post is still new after the durable absent
            # pair observation. Wall-clock timestamps are not admission proof.
            before_enrollment = self.ledger.actor(parent.key)["enrolled_at"] - 86_400_000
            first = self.native_post(channel, "First post while plugin is stopped", create_at=before_enrollment)
            second = self.native_post(channel, "Second missed reply", root=first["id"])
        finally:
            self.call("/api/v4/plugins/murmur/enable", {})
        self.assertLess(first["create_at"], self.ledger.actor(parent.key)["enrolled_at"])
        recovered = self.sync_until(parent.key, [first["id"], second["id"]])
        self.assertEqual({p["id"] for p in recovered if p["notification_eligible"]}, {first["id"], second["id"]})
        before = self.attention(parent.key)
        self.call("/api/v4/plugins/murmur/disable", {})
        self.call("/api/v4/plugins/murmur/enable", {})
        self.reopen()
        self.runtime.sync(parent.key)
        self.assertEqual(self.attention(parent.key), before)
        self.assertEqual(self.native_dm(parent.key), channel)
        self.runtime.deliver(parent)
        attempt = self.ledger.pending_attempts(parent.key)[0]
        self.assertEqual(set(attempt["post_ids"]), {first["id"], second["id"]})
        self.event(parent, "submit", "recovered-human-turn", attempt["token"])
        self.event(parent, "complete", "recovered-human-turn")
        self.reopen()
        self.runtime.sync(parent.key)
        self.assertIsNone(self.runtime.deliver(parent))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM receipts WHERE actor=?", (parent.key,)).fetchone()[0], 2)

    def test_existing_pair_at_configuration_enable_is_quiet_and_later_post_stays_fresh(self):
        parent = self.identity()
        with self.operator_unconfigured():
            # This actor has never had a successful configured-pair lookup;
            # its first enable must snapshot the already-existing pair quiet.
            self.runtime.register(parent, "Initial unconfigured human-DM fixture")
            channel = self.native_dm(parent.key)
            baseline = self.native_post(channel, "Old native pair before automatic contact is enabled")
            self.runtime.sync(parent.key)
            self.assertEqual(self.ledger.conversations(parent.key), [])
        unread = self.sync_until(parent.key, [baseline["id"]])
        self.assertFalse(unread[0]["notification_eligible"])
        self.assertIsNone(self.runtime.deliver(parent))
        # Same timestamp and deliberately misleading props cannot move an ID
        # across the successfully persisted canonical baseline.
        fresh = self.native_post(channel, "New request after the baseline", create_at=baseline["create_at"],
                                 props={"notification_eligible": False})
        self.sync_until(parent.key, [fresh["id"]])
        self.assertEqual([p["id"] for p in self.ledger.notification_candidates(parent.key)], [fresh["id"]])
        self.runtime.register(parent, "Native human-DM renamed fixture")
        self.runtime.sync(parent.key)
        self.assertEqual([p["id"] for p in self.ledger.notification_candidates(parent.key)], [fresh["id"]])
        self.assertEqual(self.native_dm(parent.key), channel)

    def test_native_posts_preserve_off_threads_and_explicit_unfollow(self):
        parent = self.a
        channel = self.native_dm(parent.key)
        root = self.native_post(channel, "Native request root")
        self.sync_until(parent.key, [root["id"]])
        self.runtime.notify(parent.key, channel, "off")
        muted = self.native_post(channel, "@all muted native request")
        self.sync_until(parent.key, [muted["id"]])
        self.reopen()
        self.runtime.sync(parent.key)
        self.assertEqual(self.ledger.notification_mode(parent.key, channel)["mode"], "off")
        self.assertIsNone(self.runtime.deliver(parent))
        self.runtime.notify(parent.key, channel, "threads")
        self.runtime.follow(parent.key, root["id"])
        unrelated = self.native_post(channel, "Unfollowed native discussion")
        followed = self.native_post(channel, "Future native reply in the followed thread", root=root["id"])
        self.sync_until(parent.key, [unrelated["id"], followed["id"]])
        self.assertEqual(self.ledger.notification_mode(parent.key, channel)["mode"], "threads")
        self.assertEqual([p["id"] for p in self.ledger.notification_candidates(parent.key)], [followed["id"]])
        self.runtime.unfollow(parent.key, root["id"])
        ignored = self.native_post(channel, "Native reply after explicit unfollow", root=root["id"])
        self.sync_until(parent.key, [ignored["id"]])
        self.assertTrue(self.ledger.thread_attention(parent.key, channel, root["id"])["explicit_unfollow"])
        self.assertIsNone(self.runtime.deliver(parent))
        self.assertEqual(self.ledger.attempts(parent.key), [])
        self.assertEqual(self.runtime.inbox(parent.key)[0]["unread_count"], 5)

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
