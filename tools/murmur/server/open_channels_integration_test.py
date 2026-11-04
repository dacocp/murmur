#!/usr/bin/env python3
"""Real server/ledger acceptance, explicitly isolated from the live installation.

Set MURMUR_TEST_STATE to bootstrap.py's marked --test-state directory. All
identities are server-only fixtures; no native client or daemon is launched.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.fixture import load_fixture
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator, actor_key
from murmur.ledger import Ledger


class OpenChannelsIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("MURMUR_TEST_STATE"):
            raise unittest.SkipTest("Requires an explicitly isolated MURMUR_TEST_STATE")
        fixture = load_fixture(require_admin=True)
        cls.state = fixture.state
        cls.base = fixture.origin
        cls.backend = Backend(fixture.coordinator["server_url"], fixture.coordinator["token"])
        cls.admin = fixture.admin

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / "ledger.sqlite3")
        self.addCleanup(self.ledger.close)
        self.coordinator = Coordinator(self.ledger, self.backend)
        self.parent = self.enroll("parent")
        self.peer = self.enroll("peer")
        self.visitor = self.enroll("visitor")

    def identity(self, parent=None):
        return {"provider": "codex", "session_id": str(uuid.uuid4()), "parent_known": True,
                "parent_session_id": parent.split(":", 1)[1] if parent else None,
                "target": {}, "evidence": {}}

    def enroll(self, label, parent=None, inherit=None):
        identity = self.identity(parent)
        options = {"inherit": inherit} if inherit else {}
        self.coordinator.register(identity, "Isolated acceptance " + label, **options)
        return actor_key(identity)

    def native(self, path, data=None, method=None):
        request = Request(self.base + "/api/v4" + path,
                          data=json.dumps(data).encode() if data is not None else None,
                          headers={"Authorization": "Bearer " + self.admin["token"],
                                   "Content-Type": "application/json"}, method=method)
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def channel(self, name="Acceptance public channel", peers=None):
        result = self.coordinator.mutate(self.parent, "conversation", kind="channel", name=name,
                                         purpose="Check intentional public collaboration",
                                         members=[self.peer] if peers is None else peers)
        return result["conversation"]["id"]

    def post(self, actor, channel, text, root=None):
        return self.coordinator.mutate(actor, "post", channel_id=channel,
                                       message=text, root_id=root or "")["post"]

    def sql_counts(self, actor):
        return {table: self.ledger.db.execute(
            f"SELECT count(*) FROM {table} WHERE actor=?", (actor,)).fetchone()[0]
            for table in ("conversations", "receipts", "attempts", "outbound")}

    def test_public_preview_is_quiet_and_saved_only_does_not_join(self):
        channel = self.channel(peers=[])
        root = self.post(self.parent, channel, "Inspect this before joining")
        before = self.sql_counts(self.visitor)
        with self.assertRaises(BackendError) as retired_route:
            self.backend.request("GET", "/groups", actor=self.visitor)
        self.assertEqual(retired_route.exception.status, 404)
        with self.assertRaises(BackendError) as retired_kind:
            self.backend.request("POST", "/conversations", {
                "actor": self.visitor, "op_id": str(uuid.uuid4()),
                "kind": "group", "name": "Retired spelling", "members": []})
        self.assertEqual(retired_kind.exception.status, 400)
        page = self.backend.request("GET", "/channels", actor=self.visitor, query=channel, limit=1)
        self.assertEqual([entry["id"] for entry in page["channels"]], [channel])
        self.assertNotIn("groups", page)
        self.assertEqual(page["channels"][0]["kind"], "channel")
        self.assertIsNone(page["channels"][0]["role"])
        self.assertEqual(page["channels"][0]["purpose"], "Check intentional public collaboration")
        self.assertEqual(self.coordinator.history(self.visitor, channel)["posts"][0]["id"], root["id"])
        threads = self.coordinator.threads(self.visitor, channel)
        self.assertIsNone(threads["threads"][0]["unread_count"])
        self.assertEqual(self.coordinator.show(self.visitor, root["id"])["id"], root["id"])
        self.assertEqual(self.sql_counts(self.visitor), before)
        with self.assertRaises(BackendError) as denied:
            self.post(self.visitor, channel, "Must explicitly join")
        self.assertEqual(denied.exception.status, 403)
        self.coordinator.set_saved(self.visitor, root["id"], True)
        row = next(row for row in self.coordinator.inbox(self.visitor) if row["id"] == channel)
        self.assertEqual((row["role"], row["muted"], row["unread_count"], row["starred_count"]),
                         (None, True, 0, 1))
        self.assertEqual(self.sql_counts(self.visitor)["conversations"], before["conversations"])
        self.assertEqual(self.sql_counts(self.visitor)["receipts"], before["receipts"])
        native = self.native("/channels/" + channel)
        self.assertEqual(native["type"], "O")
        unmanaged = self.native("/channels", {"team_id": native["team_id"], "type": "O",
            "name": "unmanaged-" + uuid.uuid4().hex[:16], "display_name": "Unmanaged fixture"})
        with self.assertRaises(BackendError) as denied:
            self.coordinator.history(self.visitor, unmanaged["id"])
        self.assertEqual(denied.exception.status, 404)

    def test_join_tracks_new_replies_not_old_edits_and_survives_restart(self):
        channel = self.channel()
        old = self.post(self.peer, channel, "Old request after visitor enrollment")
        join_id = str(uuid.uuid4())
        self.backend.mutate("join", self.visitor, join_id, channel_id=channel)
        self.coordinator.sync(self.visitor)
        self.assertEqual(self.coordinator.inbox(self.visitor), [])
        self.native("/posts/" + old["id"], {"id": old["id"], "message": "Old request edited"}, "PUT")
        new = self.post(self.peer, channel, "New reply to old root", old["id"])
        self.backend.mutate("join", self.visitor, join_id, channel_id=channel)
        self.backend.mutate("join", self.visitor, str(uuid.uuid4()), channel_id=channel)
        self.assertEqual([post["id"] for post in self.coordinator.read(self.visitor, channel)], [new["id"]])
        self.native("/plugins/murmur/disable", {}, "POST")
        self.native("/plugins/murmur/enable", {}, "POST")
        self.assertEqual([post["id"] for post in self.coordinator.read(self.visitor, channel)], [new["id"]])
        self.ledger.mark_read(self.visitor, [new["id"]])
        self.coordinator.mutate(self.visitor, "leave", channel_id=channel)
        later = self.post(self.peer, channel, "Readable archive after departure")
        self.assertEqual(self.coordinator.inbox(self.visitor), [])
        self.assertEqual(self.coordinator.show(self.visitor, later["id"])["id"], later["id"])

    def test_inherited_channels_and_dms_are_reference_without_attention(self):
        channel = self.channel()
        dm = self.coordinator.mutate(self.parent, "conversation", kind="dm", members=[self.peer])["conversation"]["id"]
        old = self.post(self.peer, dm, "Parent-only DM pending")
        child = self.enroll("fresh child", self.parent)
        grandchild = self.enroll("fresh grandchild", child)
        later_channel = self.channel("Channel created after fork")
        for room in (channel, later_channel, dm):
            self.post(self.peer, room, "Later parent conversation")
        for actor in (child, grandchild):
            self.assertEqual(self.coordinator.inbox(actor), [])
            self.assertEqual(self.sql_counts(actor)["receipts"], 0)
            self.assertEqual(self.coordinator.show(actor, old["id"])["id"], old["id"])
            self.assertIsNone(self.ledger.create_attempt(actor, "fixture"))
        with self.assertRaises(BackendError) as denied:
            self.backend.post(self.visitor, old["id"])
        self.assertEqual(denied.exception.status, 403)
        self.coordinator.mutate(child, "join", channel_id=channel)
        reply = self.post(self.peer, channel, "Full member now has new work")
        self.assertEqual([post["id"] for post in self.coordinator.read(child, channel)], [reply["id"]])
        self.assertEqual(self.coordinator.inbox(grandchild), [])

    def test_pending_import_is_independent_quiet_and_not_a_later_mirror(self):
        channel = self.channel()
        pending = self.post(self.peer, channel, "Unread parent request")
        saved = self.post(self.peer, channel, "Read but saved parent context")
        dm = self.coordinator.mutate(self.parent, "conversation", kind="dm", members=[self.peer])["conversation"]["id"]
        dm_pending = self.post(self.peer, dm, "Unread parent DM request")
        self.coordinator.sync(self.parent)
        self.ledger.mark_read(self.parent, [saved["id"]])
        self.coordinator.set_saved(self.parent, saved["id"], True)
        identity = self.identity(self.parent)
        child = actor_key(identity)
        self.coordinator.register(identity, "Isolated pending child", inherit="pending")
        unread = self.coordinator.read(child, channel)
        self.assertEqual([post["id"] for post in unread], [pending["id"]])
        self.assertEqual([post["id"] for post in self.coordinator.read(child, dm)], [dm_pending["id"]])
        self.assertEqual(next(row for row in self.coordinator.inbox(child) if row["id"] == channel)["starred_count"], 1)
        self.assertIsNone(self.ledger.create_attempt(child, "fixture"))
        with self.assertRaises(BackendError) as denied:
            self.post(child, dm, "Child cannot impersonate parent")
        self.assertEqual(denied.exception.status, 403)
        self.ledger.mark_read(child, [pending["id"], dm_pending["id"]])
        self.coordinator.set_saved(child, saved["id"], False)
        later = self.post(self.peer, channel, "Parent-only later work")
        self.coordinator.register(identity, "Renamed child")
        self.assertEqual(self.coordinator.inbox(child), [])
        parent_unread = self.coordinator.read(self.parent, channel)
        self.assertEqual({post["id"] for post in parent_unread}, {pending["id"], later["id"]})
        self.assertEqual([item["post_id"] for item in self.backend.saved(self.parent)], [saved["id"]])

    def test_channel_import_joins_parent_full_channels_preserving_mute(self):
        channel = self.channel()
        self.coordinator.sync(self.parent)
        self.ledger.set_mute(self.parent, channel, True)
        child = self.enroll("subscribed child", self.parent, "pending-and-channels")
        state = next(row for row in self.ledger.conversations(child) if row["id"] == channel)
        self.assertEqual((state["role"], state["muted"]), ("member", True))
        new = self.post(self.peer, channel, "Muted member still tracks work")
        self.assertEqual([post["id"] for post in self.coordinator.read(child, channel)], [new["id"]])
        self.assertIsNone(self.ledger.create_attempt(child, "fixture"))
        later = self.channel("Parent later membership is quiet")
        self.post(self.peer, later, "Do not autojoin this")
        self.coordinator.sync(child)
        role = next(row["role"] for row in self.ledger.conversations(child) if row["id"] == later)
        self.assertEqual(role, "observer")
        self.assertEqual([post["id"] for post in self.coordinator.read(child, later)], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
