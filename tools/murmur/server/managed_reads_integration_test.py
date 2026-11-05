#!/usr/bin/env python3
"""Real managed-read acceptance on an explicitly marked disposable server.

Uses synthetic server-only native/manual identities and an isolated ledger. The
legacy-private seed briefly disables only this fixture's plugin and changes one
validated fixture metadata row; no live installation is selected implicitly.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger
from murmur.native import NativeIdentity
from server.attachments_integration_test import fixtures, raw, upload
from server.fixture import fixture_arguments, load_fixture
from server.operator_integration_test import AGENT, Server, table_snapshot
import release


def uid():
    return str(uuid.uuid4())


def agent_path(path, actor, **query):
    return AGENT + path + "?" + urlencode({"actor": actor, **query})


def capability(backend):
    result = backend.request("GET", "/health")["capabilities"]["managed_conversation_reads"]
    assert type(result["version"]) is int and result["version"] == 1
    assert type(result["enabled"]) is bool
    return result


def policy(server, backend, value):
    config = server.native("/config")
    plugin = config["PluginSettings"]["Plugins"].setdefault("murmur", {})
    if value is None:
        plugin.pop("allowmanagedconversationreads", None)
    else:
        plugin["allowmanagedconversationreads"] = value
    server.native("/config", config, method="PUT")
    deadline = time.monotonic() + 15
    while capability(backend)["enabled"] is not bool(value):
        assert time.monotonic() < deadline, "Fixture policy change did not become visible"
        time.sleep(0.1)


def expect_backend(status, operation):
    try:
        operation()
    except BackendError as error:
        assert error.status == status, ("Unexpected backend failure status", status, error.status)
    else:
        raise AssertionError("Expected a denied backend operation")


def native_preferences(server, user):
    return sorted(server.native("/users/" + user + "/preferences") or [],
                  key=lambda row: json.dumps(row, sort_keys=True))


def attention(ledger, actor):
    return {table: [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM " + table + " WHERE actor=? ORDER BY rowid", (actor,))]
        for table in ("conversations", "receipts", "attempts", "outbound")}


def members(server, channels):
    return {channel: sorted(server.native("/channels/" + channel + "/members"),
                            key=lambda row: row["user_id"]) for channel in channels}


def seed_legacy_private(fixture, server, backend, channel):
    """Seed an old closed managed channel, only inside the verified fixture."""
    assert re.fullmatch(r"[a-z0-9]{26}", channel)
    command = fixture.compose + ["exec", "-T", "postgres", "psql", "-U", "murmur", "-d", "murmur",
                                 "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1"]

    def sql(statement):
        return subprocess.check_output(command, input=statement, text=True).strip()

    server.native("/plugins/murmur/disable", {}, method="POST")
    try:
        native = server.native("/channels/" + channel + "/privacy", {"privacy": "P"}, method="PUT")
        assert native["id"] == channel and native["type"] == "P"
        predicate = "collection='conversations' AND entity_key='" + channel + "'"
        previous = json.loads(sql("SELECT payload FROM murmur_metadata_entities WHERE " + predicate + ";"))
        assert previous["id"] == channel and previous["kind"] == "group" and previous["open"] is True
        changed = json.loads(sql("BEGIN; UPDATE murmur_metadata_entities SET payload=jsonb_set(payload,'{open}','false'::jsonb) WHERE " +
                                predicate + " RETURNING payload; UPDATE murmur_metadata_schema SET revision=revision+1 WHERE singleton=1; COMMIT;"))
        expected = dict(previous, open=False)
        assert changed == expected, "Fixture seed changed metadata other than its legacy open bit"
    finally:
        server.native("/plugins/murmur/enable", {}, method="POST")
    assert capability(backend)["enabled"] is False
    assert server.native("/channels/" + channel)["type"] == "P"


def run(fixture):
    server = Server(fixture.coordinator, fixture.admin)
    backend = Backend(server.base + AGENT, server.coordinator)
    source = release.source_manifest(HERE.parent)
    build = backend.request("GET", "/health")["build"]
    assert build["source_sha256"] == source["source_sha256"] and build["revision"] == source["revision"], "Fixture build differs from this acceptance source"
    original = server.native("/config")["PluginSettings"]["Plugins"].get("murmur", {}).get("allowmanagedconversationreads")
    assert capability(backend)["enabled"] is False, "Use a fresh fixture with the policy disabled by default"
    run_id = uuid.uuid4().hex[:10]
    completed = []
    with tempfile.TemporaryDirectory(prefix="murmur-managed-reads-") as directory:
        ledger = Ledger(Path(directory) / "operations.sqlite3")
        runtime = Coordinator(ledger, backend)
        try:
            def enroll(label, mode="native"):
                identity = NativeIdentity("codex", uid(), parent_known=True)
                runtime.register(identity, "Managed " + run_id + " " + label, mode=mode, inherit="fresh")
                return identity

            owner, peer, reader, manual = (enroll(label, mode) for label, mode in
                (("owner", "native"), ("peer", "native"), ("reader", "native"), ("manual", "manual")))
            readers = (reader.actor, manual.actor)
            users = {actor: ledger.actor(actor)["user_id"] for actor in
                     (owner.actor, peer.actor, *readers)}
            dm = runtime.mutate(owner.actor, "conversation", kind="dm", members=[peer.actor])["conversation"]["id"]
            public = runtime.mutate(owner.actor, "conversation", kind="channel", name="Managed public " + run_id,
                                    members=[peer.actor])["conversation"]["id"]
            private = runtime.mutate(owner.actor, "conversation", kind="channel", name="Managed private " + run_id,
                                     members=[peer.actor])["conversation"]["id"]
            human = server.operator("/dms", {"op_id": uid(), "agent_id": owner.actor})["conversation"]["id"]
            roots, replies = {}, {}
            for room in (dm, public, private, human):
                roots[room] = runtime.mutate(owner.actor, "post", channel_id=room,
                    message="Managed root " + run_id)["post"]
                replies[room] = runtime.mutate(owner.actor, "post", channel_id=room,
                    message="Managed reply " + run_id, root_id=roots[room]["id"])["post"]
            filename, body, _ = fixtures()[0]
            attachment_source = Path(directory) / filename
            attachment_source.write_bytes(body)
            file_post = runtime.send(owner.actor, dm, "Bound attachment " + run_id, files=[attachment_source])["post"]
            file_id = file_post["file_ids"][0]
            seed_legacy_private(fixture, server, backend, private)
            team = server.native("/channels/" + public)["team_id"]
            unmanaged = server.native("/channels", {"team_id": team, "type": "P", "name": "unmanaged-" + run_id,
                "display_name": "Unmanaged " + run_id}, expected=201)["id"]
            unmanaged_post = server.native("/posts", {"channel_id": unmanaged, "message": "Outside managed scope"}, expected=201)
            channels = (dm, public, private, human)
            memberships = members(server, channels)
            feeds = {}
            for actor in readers:
                runtime.sync(actor)
                feeds[actor] = backend.changes(actor)
                assert feeds[actor]["changes"] == [] and backend.conversations(actor) == []
                expect_backend(403, lambda actor=actor: runtime.show(actor, roots[dm]["id"]))
                expect_backend(403, lambda actor=actor: runtime.conversation_directory(actor))
                assert runtime.show(actor, roots[public]["id"])["id"] == roots[public]["id"]
            completed.append("disabled policy preserves public preview and denies new private access")

            policy(server, backend, True)
            assert server.operator("/bootstrap")["capabilities"]["managed_conversation_reads"] == {"version": 1, "enabled": True}
            before = table_snapshot(ledger)
            for actor in readers:
                found, cursor = [], None
                while True:
                    page = runtime.conversation_directory(actor, query=run_id, limit=1, after=cursor)
                    found.extend(page["conversations"])
                    cursor = page["next_after"]
                    if cursor is None:
                        break
                assert {row["id"] for row in found} == set(channels) and len(found) == len(channels)
                by_id = {row["id"]: row for row in found}
                for room in channels:
                    descriptor = by_id[room]
                    assert descriptor["role"] is None and "granted_at" not in descriptor
                    assert actor not in {p["id"] for p in descriptor["participants"]}
                    assert runtime.history(actor, room)["posts"][0]["id"] == roots[room]["id"]
                    threads = runtime.threads(actor, room)
                    assert roots[room]["id"] in {t["id"] for t in threads["threads"]}
                    assert all(t["unread_count"] is None for t in threads["threads"])
                    assert runtime.show(actor, replies[room]["id"])["id"] == replies[room]["id"]
                    page = runtime.history(actor, room, root=roots[room]["id"])
                    assert {p["id"] for p in page["posts"]} == {roots[room]["id"], replies[room]["id"]}
                assert (by_id[dm]["kind"], by_id[dm]["open"], by_id[dm]["visibility"]) == ("dm", False, "private")
                assert (by_id[private]["kind"], by_id[private]["open"], by_id[private]["visibility"]) == ("channel", False, "legacy-private")
                assert {p["id"] for p in by_id[dm]["participants"]} == {owner.actor, peer.actor}
                assert {(p["kind"], p["id"]) for p in by_id[human]["participants"]} == {("agent", owner.actor), ("human", server.user_id)}
                selected = runtime.conversation_directory(actor, query=run_id, kind="dm")["conversations"]
                assert {row["id"] for row in selected} == {dm, human}
                selected = runtime.conversation_directory(actor, query=run_id, agent=peer.actor)["conversations"]
                assert {row["id"] for row in selected} == {dm, public, private}
                assert runtime.conversation_directory(actor, query="absent-" + run_id)["conversations"] == []
                info = backend.file_info(actor, file_post["id"], file_id)
                assert info["size"] == len(body)
                output = Path(directory) / (actor.split(":", 1)[1] + ".png")
                runtime.download(actor, file_post["id"], file_id, output)
                assert output.read_bytes() == body
            assert table_snapshot(ledger) == before, "Quiet discovery/history/thread/file reads mutated the local ledger"
            completed.append("native and manual enrolled readers browse managed DM/private/public/human content and files quietly")

            for actor in readers:
                baseline = attention(ledger, actor)
                untouched = {user: native_preferences(server, user) for other, user in users.items() if other != actor}
                runtime.set_saved(actor, roots[dm]["id"], True)
                saved = backend.saved(actor)
                assert [row["post_id"] for row in saved] == [roots[dm]["id"]]
                row = next(row for row in runtime.inbox(actor) if row["id"] == dm)
                assert row["role"] is None and row["muted"] is True and row["unread_count"] == 0 and row["starred_count"] == 1
                assert attention(ledger, actor) == baseline
                for user, expected in untouched.items():
                    assert native_preferences(server, user) == expected, "A star modified another user's preferences"
                server.call(agent_path("/saved/" + roots[dm]["id"], actor, read_version=1),
                    {"actor": actor, "starred": True, "user_id": users[owner.actor]}, token=server.coordinator, expected=400)
                runtime.set_saved(actor, roots[dm]["id"], False)
            completed.append("own bookmarks remain quiet, saved-only and independent")

            for actor in readers:
                for room in (dm, private, public):
                    server.call(AGENT + "/posts", {"actor": actor, "op_id": uid(), "channel_id": room,
                        "message": "Must remain read-only", "root_id": roots[room]["id"]}, token=server.coordinator, expected=403)
                    upload(server, actor, room, filename, body, expected=403)
                for room in (dm, private):
                    server.call(AGENT + "/conversations/" + room + "/join", {"actor": actor, "op_id": uid()},
                                token=server.coordinator, expected=403)
                expect_backend(404, lambda actor=actor: runtime.history(actor, unmanaged))
                expect_backend(404, lambda actor=actor: runtime.show(actor, unmanaged_post["id"]))
                server.call(agent_path("/conversations/" + public + "/posts", actor, read_version=1, before=roots[dm]["id"]),
                            token=server.coordinator, expected=400)
                server.call(agent_path("/threads/" + replies[dm]["id"], actor, read_version=1), token=server.coordinator, expected=400)
                for anchor in (roots[dm]["id"], unmanaged_post["id"]):
                    raw(server, agent_path("/posts/" + anchor + "/files/" + file_id, actor, read_version=1),
                        token=server.coordinator, expected=404)
            completed.append("read access grants no posting/upload/private-join or unmanaged/substituted-content access")

            unknown = "codex:" + uid()
            for path in ("/conversations/directory", "/posts/" + roots[dm]["id"], "/threads/" + roots[dm]["id"], "/saved"):
                server.call(agent_path(path, unknown, read_version=1), token=server.coordinator, expected=403)
            file_path = "/posts/" + file_post["id"] + "/files/" + file_id
            for path in ("/posts/" + roots[dm]["id"], "/conversations/" + dm + "/posts", "/threads/" + roots[dm]["id"], "/saved", file_path + "/info", file_path):
                server.call(agent_path(path, reader.actor, read_version=2), token=server.coordinator, expected=409)
                server.call(agent_path(path, reader.actor) + "&read_version=1&read_version=1", token=server.coordinator, expected=409)
            server.call(agent_path("/posts/" + roots[dm]["id"], reader.actor), token=server.coordinator, expected=409)
            assert server.call(agent_path("/posts/" + roots[dm]["id"], owner.actor), token=server.coordinator)["post"]["id"] == roots[dm]["id"]
            server.call(agent_path("/conversations/directory", reader.actor, read_version=2), token=server.coordinator, expected=409)
            server.call(agent_path("/conversations/directory", reader.actor, read_version=1, kind="invalid"), token=server.coordinator, expected=400)
            page = runtime.conversation_directory(reader.actor, query=run_id, limit=1)
            assert page["next_after"] is not None
            expect_backend(400, lambda: runtime.conversation_directory(reader.actor, query="other", limit=1, after=page["next_after"]))
            expect_backend(400, lambda: runtime.conversation_directory(manual.actor, query=run_id, limit=1, after=page["next_after"]))
            completed.append("unenrolled callers, invalid versions and cross-scope directory cursors fail visibly")

            assert members(server, channels) == memberships, "Quiet reads changed native member state"
            runtime.mutate(owner.actor, "post", channel_id=dm, message="Later private arrival " + run_id)
            for actor in readers:
                runtime.sync(actor)
                assert backend.conversations(actor) == [] and runtime.inbox(actor) == []
                assert not ledger.notification_candidates(actor) and ledger.create_attempt(actor, "fixture-quiet") is None
                assert all(not rows for rows in attention(ledger, actor).values())
                current = backend.changes(actor, feeds[actor]["cursor"], feeds[actor]["generation"])
                assert current["changes"] == [] and current["generation"] == feeds[actor]["generation"]
            # A real DM arrival increments native mention counters. Membership,
            # roles and notification preferences must still remain unchanged.
            fields = ("channel_id", "user_id", "roles", "explicit_roles", "scheme_user", "scheme_admin", "scheme_guest", "notify_props")
            current_members = members(server, channels)
            for channel in channels:
                expected = [{key: row.get(key) for key in fields} for row in memberships[channel]]
                actual = [{key: row.get(key) for key in fields} for row in current_members[channel]]
                assert actual == expected, "Reading implicitly changed native membership or notification preferences"
            completed.append("later arrivals do not create tracking, receipts, announcements or wake candidates")

            runtime.set_saved(reader.actor, roots[dm]["id"], True)
            policy(server, backend, False)
            expect_backend(403, lambda: runtime.show(reader.actor, roots[dm]["id"]))
            assert backend.saved(reader.actor) == [{"post_id": roots[dm]["id"], "unavailable": True}]
            runtime.set_saved(reader.actor, roots[dm]["id"], False)
            assert backend.saved(reader.actor) == []
            assert runtime.show(reader.actor, roots[public]["id"])["id"] == roots[public]["id"]
            completed.append("policy revocation removes shared content while preserving public and own-unstar behavior")
            return {"passed": completed, "synthetic_modes": ["native", "manual"], "native_sessions_started": 0,
                    "legacy_private_seed": "native P plus validated fixture-only SQL open=false",
                    "reader_tracking_rows": {actor: {name: len(rows) for name, rows in attention(ledger, actor).items()} for actor in readers},
                    "fixture_project": fixture.project, "fixture_origin": fixture.origin,
                    "revision": source["revision"], "source_sha256": source["source_sha256"]}
        finally:
            policy(server, backend, original)
            ledger.close()


def main():
    if not __debug__:
        raise SystemExit("Assertions must remain enabled for acceptance")
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    parser.add_argument("--report", type=Path, help="Optional JSON result path; contains no credentials")
    args = parser.parse_args()
    fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
    result = run(fixture)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
        args.report.chmod(0o600)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
