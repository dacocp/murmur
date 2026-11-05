#!/usr/bin/env python3
"""Thread-attention acceptance against an explicitly marked disposable server.

Synthetic enrolled actors use a real HTTP backend and isolated SQLite ledgers.
The transport records fake queue acceptance only; no native model is started.
The uncertainty case leaves a real successful HTTP response unread and posts
a real human reply before reconnecting the ledger and reconciling its intent.
"""

import argparse
from dataclasses import asdict
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import release
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError
from murmur.native import DeliveryResult, NativeIdentity
from server.attachments_integration_test import fixtures
from server.fixture import fixture_arguments, load_fixture
from server.operator_integration_test import AGENT, Server


def uid():
    return str(uuid.uuid4())


class QueueOnly:
    def __init__(self):
        self.calls = []

    def deliver(self, identity, message):
        self.calls.append((identity.actor, message))
        return DeliveryResult("accepted", "Fixture queue acceptance; no model admission", identity.target)


class CountingBackend(Backend):
    def __init__(self, *args):
        super().__init__(*args)
        self.post_requests = 0

    def mutate(self, action, *args, **values):
        if action == "post":
            self.post_requests += 1
        return super().mutate(action, *args, **values)


class OfflineBackend(Backend):
    def __init__(self, *args):
        super().__init__(*args)
        self.requests = 0

    def request(self, *args, **kwargs):
        self.requests += 1
        raise BackendError("Fixture network is unavailable")


class LostPostResponse(CountingBackend):
    """Discard a real response only after independent HTTP proves commitment."""
    armed = False

    def mutate(self, action, actor, op_id, **payload):
        if action != "post" or not self.armed:
            return super().mutate(action, actor, op_id, **payload)
        self.armed = False
        self.post_requests += 1
        with sqlite3.connect("file:" + self.ledger.path + "?mode=ro", uri=True) as other:
            self.frozen_boundary = other.execute(
                "SELECT after_seq FROM auto_follow_intents WHERE op_id=? AND state='pending'", (op_id,)).fetchone()[0]
            assert other.execute("SELECT state FROM outbound WHERE op_id=?", (op_id,)).fetchone()[0] == "pending"
        parsed = urlsplit(self.url)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=30)
        path = parsed.path + "/posts"
        connection.request("POST", path, json.dumps(dict(actor=actor, op_id=op_id, **payload)),
                           {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    proof = self.operation(actor, op_id)
                except BackendError as error:
                    if error.status != 404:
                        raise
                    proof = {}
                if proof.get("status") == "complete":
                    break
                assert time.monotonic() < deadline, "Real post did not commit before response discard"
                time.sleep(0.05)
            self.committed_post = proof["result"]["post_id"]
            self.fast_reply = self.server.operator("/posts", {"op_id": uid(), "channel_id": payload["channel_id"],
                "root_id": payload.get("root_id") or self.committed_post, "message": "Human fast reply before response discard"})["post"]
            self.before_discard()
        finally:
            # No getresponse/read: the canonical write and reply are proved by
            # separate real requests while this successful response is unread.
            connection.close()
        raise BackendError("Fixture discarded the real successful post response", uncertain=True)


class LocalStop(RuntimeError):
    pass


class StopBeforeFollowLedger(Ledger):
    stop_before_follow = False

    def complete_auto_follow(self, op_id, post):
        if self.stop_before_follow:
            self.stop_before_follow = False
            raise LocalStop("Simulated process stop after durable post completion")
        return super().complete_auto_follow(op_id, post)


class Case:
    def __init__(self, fixture, name, *, ledger_type=Ledger, backend_type=CountingBackend):
        self.server = Server(fixture.coordinator, fixture.admin)
        self.temp = tempfile.TemporaryDirectory(prefix="murmur-thread-http-")
        self.path = Path(self.temp.name) / "operations.sqlite3"
        self.backend = backend_type(fixture.coordinator["server_url"], fixture.coordinator["token"])
        self.transport = QueueOnly()
        self.ledger = ledger_type(self.path)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)
        self.name = name + " " + uuid.uuid4().hex[:8]
        self.owner = self.enroll("owner")
        self.peer = self.enroll("peer")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.ledger.close()
        self.temp.cleanup()

    def enroll(self, label, parent=None, inherit="fresh"):
        identity = NativeIdentity("codex", uid(), parent_known=True,
                                  parent_session_id=parent.session_id if parent else None)
        self.runtime.register(identity, self.name + " " + label, inherit=inherit)
        return identity

    def channel(self, label="work"):
        channel = self.runtime.mutate(self.owner.actor, "conversation", kind="channel", name=self.name + " " + label,
                                      members=[self.peer.actor])["conversation"]["id"]
        self.runtime.sync(self.owner.actor)
        self.runtime.sync(self.peer.actor)
        return channel

    def dm(self):
        channel = self.runtime.mutate(self.owner.actor, "conversation", kind="dm",
                                      members=[self.peer.actor])["conversation"]["id"]
        self.runtime.sync(self.owner.actor)
        self.runtime.sync(self.peer.actor)
        return channel

    def post(self, channel, text, root="", *, author=None):
        return self.runtime.send((author or self.peer).actor, channel, text, root=root)["post"]

    def human(self, channel, text, root=""):
        return self.server.operator("/posts", {"op_id": uid(), "channel_id": channel, "message": text, "root_id": root})["post"]

    def candidates(self, actor, channel):
        return {p["id"] for p in self.ledger.notification_candidates(actor) if p["channel_id"] == channel}

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.backend = CountingBackend(self.backend.url, self.backend.token)
        self.runtime = Coordinator(self.ledger, self.backend, self.transport)


def modes_and_boundaries(fixture):
    with Case(fixture, "Modes") as c:
        actor, room = c.owner.actor, c.channel()
        c.runtime.notify(actor, room, "off")
        cursor = c.ledger.actor(actor)["cursor"]
        c.server.native("/plugins/murmur/disable", {}, method="POST")
        try:
            old = c.server.native("/posts", {"channel_id": room, "message": "Missed canonical root"}, expected=201)
            prior = c.server.native("/posts", {"channel_id": room, "root_id": old["id"], "message": "Missed old reply"}, expected=201)
        finally:
            c.server.native("/plugins/murmur/enable", {}, method="POST")
        mode = c.runtime.notify(actor, room, "threads")
        assert mode["after_seq"] > cursor
        followed = c.runtime.follow(actor, prior["id"])
        assert followed["root_id"] == old["id"] and followed["after_seq"] >= mode["after_seq"]
        c.runtime.sync(actor)
        assert c.candidates(actor, room) == set()
        assert {old["id"], prior["id"]} <= {p["id"] for p in c.ledger.unread_posts(actor, room)}
        future = c.post(room, "Agent future reply", old["id"])
        human = c.human(room, "Human future reply", old["id"])
        unrelated = c.post(room, "Unfollowed future root")
        c.runtime.sync(actor)
        assert c.candidates(actor, room) == {future["id"], human["id"]}
        assert c.runtime.deliver(c.owner).status == "accepted" and len(c.transport.calls) == 1
        attempt = c.ledger.attempts(actor)[0]
        assert set(attempt["post_ids"]) == {future["id"], human["id"]}
        frozen = c.ledger.attempt(attempt["token"])
        unread = {p["id"] for p in c.ledger.unread_posts(actor, room)}
        c.runtime.notify(actor, room, "off")
        quiet = c.post(room, "Off suppresses even @all", old["id"])
        c.runtime.sync(actor)
        assert c.runtime.deliver(c.owner) is None and len(c.transport.calls) == 1
        assert c.ledger.attempt(attempt["token"]) == frozen
        assert unread | {quiet["id"]} <= {p["id"] for p in c.ledger.unread_posts(actor, room)}
        c.runtime.notify(actor, room, "all")
        c.runtime.sync(actor)
        assert c.candidates(actor, room) == set()
        all_post = c.post(room, "All accepts a new unrelated discussion")
        c.runtime.sync(actor)
        assert c.candidates(actor, room) == {all_post["id"]}
        assert unrelated["id"] not in c.candidates(actor, room)
        return {"old_cursor": cursor, "follow_after_seq": followed["after_seq"], "queue_acceptances": 1}


def automatic_and_offline(fixture):
    with Case(fixture, "Automatic") as c:
        actor, room = c.owner.actor, c.dm()
        peer_root = c.post(room, "Existing peer root")
        c.runtime.notify(actor, room, "threads")
        old_reply = c.post(room, "Before participating", peer_root["id"])
        c.runtime.sync(actor)
        c.runtime.history(actor, room)
        c.runtime.show(actor, peer_root["id"])
        c.runtime.threads(actor, room)
        c.runtime.set_saved(actor, peer_root["id"], True)
        assert c.runtime.following(actor) == []
        c.runtime.set_saved(actor, peer_root["id"], False)
        own = c.post(room, "Own new root", author=c.owner)
        answer = c.post(room, "Own reply", peer_root["id"], author=c.owner)
        filename, body, _ = fixtures()[0]
        attachment = Path(c.temp.name) / filename
        attachment.write_bytes(body)
        file_only = c.runtime.send(actor, room, "", files=[attachment])["post"]
        assert file_only["message"] == "" and len(file_only["file_ids"]) == 1
        assert {f["root_id"] for f in c.runtime.following(actor, room)} == {own["id"], peer_root["id"], file_only["id"]}
        assert c.ledger.resolve_thread_alias(actor, answer["id"])["root_id"] == peer_root["id"]
        future = c.post(room, "After own reply", peer_root["id"])
        root_future = c.post(room, "After own new root", own["id"])
        file_future = c.human(room, "After own file-only root", file_only["id"])
        c.runtime.sync(actor)
        assert c.candidates(actor, room) == {future["id"], root_future["id"], file_future["id"]}
        assert old_reply["id"] not in c.candidates(actor, room)
        c.runtime.unfollow(actor, answer["id"])
        c.post(room, "Own reply after explicit unfollow", peer_root["id"], author=c.owner)
        assert not c.ledger.thread_attention(actor, room, peer_root["id"])["followed"]
        assert c.ledger.thread_attention(actor, room, peer_root["id"])["explicit_unfollow"]
        offline = OfflineBackend(c.backend.url, c.backend.token)
        local = Coordinator(c.ledger, offline, c.transport)
        local.notify(actor, room, "off")
        local.unfollow(actor, file_only["id"])
        assert offline.requests == 0
        c.runtime.follow(actor, answer["id"])
        assert c.ledger.thread_attention(actor, room, peer_root["id"])["followed"]
        return {"auto_follow_forms": ["root", "reply", "file-only"], "offline_requests": offline.requests}


def lost_response(fixture):
    with Case(fixture, "Lost response", backend_type=LostPostResponse) as c:
        actor, room = c.owner.actor, c.channel()
        c.runtime.notify(actor, room, "threads")
        backend = c.backend
        backend.ledger, backend.server = c.ledger, c.server
        backend.before_discard = lambda: c.runtime.sync(actor)
        backend.armed = True
        text = "Exactly one real lost-response root " + uid()
        try:
            c.runtime.send(actor, room, text)
        except BackendError as error:
            assert error.uncertain
        else:
            raise AssertionError("The successful response was not discarded")
        intent = c.ledger.pending_auto_follows(actor)[0]
        assert intent["after_seq"] == backend.frozen_boundary
        assert c.ledger.outbound(intent["op_id"])["state"] == "uncertain"
        assert backend.fast_reply["id"] not in c.candidates(actor, room)
        c.reopen()
        c.runtime.reconcile_outbound(actor)
        assert c.ledger.outbound(intent["op_id"])["state"] == "complete"
        assert c.ledger.pending_auto_follows(actor) == [] and c.backend.post_requests == 0
        assert c.ledger.thread_attention(actor, room, backend.committed_post)["after_seq"] == backend.frozen_boundary
        assert backend.fast_reply["id"] in c.candidates(actor, room)
        c.runtime.reconcile_outbound(actor)
        posts = c.backend.posts(actor, room, limit=100)["posts"]
        assert sum(p["message"] == text for p in posts) == 1 and c.backend.post_requests == 0
        return {"real_response_discarded": True, "human_reply_preceded_recovery": True,
                "original_boundary": backend.frozen_boundary, "recovery_post_requests": 0}


def completed_recovery_and_failure(fixture):
    with Case(fixture, "Completion", ledger_type=StopBeforeFollowLedger) as c:
        actor, room = c.owner.actor, c.channel()
        c.runtime.notify(actor, room, "threads")
        c.ledger.stop_before_follow = True
        text = "Committed before local sidecar stop " + uid()
        try:
            c.runtime.send(actor, room, text)
        except LocalStop:
            pass
        else:
            raise AssertionError("The local completion interruption did not occur")
        intent = c.ledger.pending_auto_follows(actor)[0]
        operation = c.ledger.outbound(intent["op_id"])
        assert operation["state"] == "complete" and c.runtime.following(actor, room) == []
        c.reopen()
        c.runtime.reconcile_outbound(actor)
        assert c.backend.post_requests == 0 and c.ledger.pending_auto_follows(actor) == []
        assert {f["root_id"] for f in c.runtime.following(actor, room)} == {operation["result"]["post_id"]}
        assert sum(p["message"] == text for p in c.backend.posts(actor, room, limit=100)["posts"]) == 1
        other_room = c.channel("other")
        foreign = c.post(other_room, "Wrong destination root")
        before = c.runtime.following(actor, room)
        try:
            c.runtime.send(actor, room, "Must fail binding", root=foreign["id"])
        except BackendError as error:
            assert error.status == 400 and not error.uncertain
        else:
            raise AssertionError("A root from another channel was accepted")
        assert c.runtime.following(actor, room) == before and c.ledger.pending_auto_follows(actor) == []
        assert any(o["state"] == "failed" for o in c.ledger.outbounds(actor))
        return {"completed_sidecar_recovered": True, "known_failure_followed": False, "recovery_post_requests": 0}


def leave_and_forks(fixture):
    with Case(fixture, "Membership") as c:
        actor, room = c.owner.actor, c.channel()
        root = c.post(room, "Membership root")
        c.runtime.notify(actor, room, "threads")
        c.runtime.follow(actor, root["id"])
        c.runtime.mutate(actor, "leave", channel_id=room)
        c.runtime.sync(actor)
        assert c.runtime.following(actor) == []
        assert not c.ledger.thread_attention(actor, room, root["id"])["followed"]
        absent = c.post(room, "While absent", root["id"])
        c.runtime.mutate(actor, "join", channel_id=room)
        # A confirmed join must refresh local participation itself. Do not hide
        # an observer-to-member activation bug behind a separate feed sync.
        assert c.runtime.following(actor, room) == []
        assert c.ledger.notification_mode(actor, room)["mode"] == "threads"
        c.runtime.follow(actor, root["id"])
        later = c.post(room, "After explicit follow on return", root["id"])
        c.runtime.sync(actor)
        assert c.candidates(actor, room) == {later["id"]} and absent["id"] not in c.candidates(actor, room)
    with Case(fixture, "Forks") as c:
        actor, room = c.owner.actor, c.channel()
        root = c.post(room, "Parent followed root")
        c.runtime.notify(actor, room, "threads")
        c.runtime.follow(actor, root["id"])
        c.runtime.set_saved(actor, root["id"], True)
        pending = c.post(room, "Parent current pending reply", root["id"])
        c.runtime.sync(actor)
        parent_receipts = [tuple(r) for r in c.ledger.db.execute("SELECT * FROM receipts WHERE actor=? ORDER BY arrival", (actor,))]
        fresh = c.enroll("fresh", c.owner, "fresh")
        imported = c.enroll("pending", c.owner, "pending")
        subscribed = c.enroll("subscribed", c.owner, "pending-and-channels")
        for child in (fresh, imported):
            assert c.runtime.following(child.actor) == [] and c.ledger.notification_candidates(child.actor) == []
        assert c.backend.saved(fresh.actor) == []
        assert root["id"] in {p["post_id"] for p in c.backend.saved(imported.actor)}
        plan = c.ledger.onboarding(subscribed.actor)
        cutoff = plan["snapshot"]["attention_boundaries"][room]
        assert c.ledger.notification_mode(subscribed.actor, room)["mode"] == "threads"
        assert c.ledger.thread_attention(subscribed.actor, room, root["id"])["after_seq"] == cutoff
        assert c.ledger.notification_candidates(subscribed.actor) == []
        # This explicitly seeded pre-feature frozen plan contains only its old
        # join/mute contract; the real registration runner must not reinterpret it.
        old = NativeIdentity("codex", uid(), parent_session_id=c.owner.session_id, parent_known=True)
        c.ledger.enroll(old.actor, asdict(old), c.name + " legacy", onboarding=("pending-and-channels", actor))
        old_plan = c.ledger.onboarding(old.actor)
        frozen = c.ledger.capture_pending(old.actor, old_plan["plan_id"])
        frozen.pop("attention_version", None)
        frozen["channels"] = [{"id": item["id"], "muted": item["muted"]} for item in frozen["channels"]]
        frozen.update(saved_ids=[root["id"]], saved_started_at=c.ledger.clock(), saved_finished_at=c.ledger.clock())
        c.ledger.db.execute("UPDATE onboarding SET snapshot_json=? WHERE actor=?", (json.dumps(frozen), old.actor))
        c.runtime.register(old, c.name + " legacy", inherit="pending-and-channels")
        assert c.ledger.onboarding(old.actor)["snapshot"] == frozen
        assert c.runtime.following(old.actor) == [] and c.ledger.notification_mode(old.actor, room)["mode"] == "all"
        c.runtime.register(old, c.name + " renamed", inherit="pending-and-channels")
        assert c.ledger.onboarding(old.actor)["snapshot"] == frozen and c.runtime.following(old.actor) == []
        assert [tuple(r) for r in c.ledger.db.execute("SELECT * FROM receipts WHERE actor=? ORDER BY arrival", (actor,))] == parent_receipts
        future = c.post(room, "After independent child boundary", root["id"])
        c.runtime.sync(subscribed.actor)
        assert c.candidates(subscribed.actor, room) == {future["id"]}
        assert pending["id"] not in c.candidates(subscribed.actor, room)
        return {"choice4_fresh_boundary": cutoff, "fresh_pending_follows": 0, "legacy_frozen_plan_unchanged": True}


def main():
    if not __debug__:
        raise SystemExit("Assertions must remain enabled for acceptance")
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
    source_manifest = release.source_manifest(HERE.parent)
    backend = Backend(fixture.coordinator["server_url"], fixture.coordinator["token"])
    health = backend.request("GET", "/health")
    assert health["build"]["revision"] == source_manifest["revision"] and health["build"]["source_sha256"] == source_manifest["source_sha256"]
    assert health["capabilities"]["attention_boundaries"] == {"version": 1}
    assert health["build"]["compatibility"]["ledger_schema"] == 5
    results = {name: check(fixture) for name, check in (
        ("canonical_boundaries_and_modes", modes_and_boundaries),
        ("automatic_follows_sticky_unfollow_and_offline", automatic_and_offline),
        ("real_lost_response_fast_human_reply_restart", lost_response),
        ("completed_sidecar_recovery_and_known_failure", completed_recovery_and_failure),
        ("leave_rejoin_and_independent_fork_choices", leave_and_forks))}
    result = {"revision": source_manifest["revision"], "source_sha256": source_manifest["source_sha256"],
              "fixture_project": fixture.project, "native_models_started": 0,
              "transport_evidence": "Fake queue acceptance only; actual model admission is separate", "passed": results}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
        args.report.chmod(0o600)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
