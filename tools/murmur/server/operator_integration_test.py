#!/usr/bin/env python3
"""Opt-in operator HTTP acceptance using fresh server-only fixtures.

Run only after the reviewed operator backend is deployed. Existing private
admin/coordinator sessions stay at their source. A temporary ledger and fake
transport never enroll or wake a real native session. This harness does not
restart the plugin, alter native settings, or exercise the separate live-agent
acceptance protocol.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import http.client
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from murmur.backend import Backend
from server.fixture import fixture_arguments, load_fixture
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger
from murmur.native import DeliveryResult, NativeIdentity


OPERATOR = "/plugins/murmur/operator/v1"
AGENT = "/plugins/murmur/v1"


def new_id():
    return str(uuid.uuid4())


class FakeNative:
    def __init__(self):
        self.calls = []

    def deliver(self, identity, message):
        self.calls.append((identity, message))
        return DeliveryResult("accepted", "Synthetic fixture transport only", identity.target)


class Server:
    def __init__(self, configuration, session):
        parsed = urlsplit(configuration.get("server_url", configuration.get("url", "")))
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("This fixture harness requires its bound HTTP(S) origin")
        if parsed.username or parsed.password:
            raise ValueError("The fixture server URL must not contain credentials")
        self.base = parsed.scheme + "://" + parsed.netloc
        self.coordinator = configuration["token"]
        self.admin = session["token"]
        self.user_id = session["user_id"]
        self.bootstrap_seconds = []

    def call(self, path, data=None, *, token=None, expected=200, method=None, headers=None,
             with_headers=False):
        values = {"Content-Type": "application/json"}
        if token:
            values["Authorization"] = "Bearer " + token
        values.update(headers or {})
        request = Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                          headers=values, method=method)
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            statuses = expected if isinstance(expected, tuple) else (expected,)
            # Never print a request/header or authentication response on failure.
            if response.status not in statuses:
                raise AssertionError(f"{request.get_method()} {path}: expected {statuses}, got {response.status}")
            content = response.read()
            result = json.loads(content) if content else None
            return (result, response.headers) if with_headers else result

    def operator(self, path, data=None, *, expected=200, headers=None):
        values = {"X-Murmur-User-Id": self.user_id}
        values.update(headers or {})
        started = time.monotonic()
        result = self.call(OPERATOR + path, data, token=self.admin, expected=expected, headers=values)
        if path == "/bootstrap":
            self.bootstrap_seconds.append(time.monotonic() - started)
        return result

    def native(self, path, data=None, **options):
        return self.call("/api/v4" + path, data, token=self.admin, **options)


def table_snapshot(ledger):
    """All rows, including attention, causal state, cursors and SQLite sequences."""
    names = [row[0] for row in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {name: sorted((tuple(row) for row in ledger.db.execute(
        'SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr) for name in names}


def native_snapshot(server, channel, users):
    return {
        "members": sorted(server.native("/channels/" + channel + "/members"), key=lambda m: m["user_id"]),
        "preferences": {user: sorted(server.native("/users/" + user + "/preferences") or [],
                                     key=lambda p: json.dumps(p, sort_keys=True)) for user in users},
    }


def check_auth(server, run_id):
    # The real plugin proxy must discard caller-supplied authenticated identity.
    for token in (None, server.coordinator):
        server.call(OPERATOR + "/bootstrap", token=token, expected=401,
                    headers={"Mattermost-User-Id": server.user_id})
    server.call(AGENT + "/agents", token=server.admin, expected=401)
    server.operator("/bootstrap", headers={"X-Murmur-User-Id": "different-account"}, expected=409)
    server.operator("/posts", {}, headers={"Content-Type": "text/plain"}, expected=415)

    # A fresh ordinary human isolates the role check from the bot exclusion.
    username = "murmur-op-check-" + run_id
    password = "Murmur-fixture-" + uuid.uuid4().hex + "aA1!"
    user = server.native("/users", {"username": username, "email": username + "@example.invalid",
                                    "password": password}, expected=201)
    assert "system_admin" not in user["roles"].split()
    result, headers = server.call("/api/v4/users/login", {"login_id": username, "password": password},
                                  with_headers=True)
    token = headers.get("Token")
    assert token and result["id"] == user["id"]
    try:
        server.call(OPERATOR + "/bootstrap", token=token, expected=403)
        server.call(OPERATOR + "/bootstrap", token=token, expected=403,
                    headers={"Mattermost-User-Id": server.user_id})
    finally:
        server.call("/api/v4/users/logout", {}, token=token)


def all_operator_posts(server, channel, limit=2):
    result, before, seen = [], None, set()
    while True:
        query = {"limit": limit}
        if before:
            query["before"] = before
        page = server.operator("/conversations/" + channel + "/posts?" + urlencode(query))
        for post in page["posts"]:
            assert post["id"] not in seen, "Operator pagination duplicated a post"
            seen.add(post["id"])
        result[0:0] = page["posts"]
        before = page["next_before"]
        if not before:
            return result


def discarded_response(server, payload):
    """Discard one response only after independent evidence proves completion."""
    parsed = urlsplit(server.base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
    connection.request("POST", OPERATOR + "/posts", json.dumps(payload), {
        "Authorization": "Bearer " + server.admin, "Content-Type": "application/json",
        "X-Murmur-User-Id": server.user_id,
    })
    try:
        deadline = time.monotonic() + 15
        while True:
            operation = server.operator("/operations/" + payload["op_id"], expected=(200, 404))
            if operation.get("status") == "complete":
                return operation
            if operation.get("status") == "uncertain" or time.monotonic() >= deadline:
                raise AssertionError("Operator operation unconfirmed; stop without reposting: " + payload["op_id"])
            time.sleep(0.1)
    finally:
        connection.close()


def rejected_send(server, payload, status):
    """An exact durable rejection is terminal and has no canonical post effect."""
    posts_path = "/channels/" + payload["channel_id"] + "/posts"
    before = server.native(posts_path)
    rejected = server.operator("/posts", payload, expected=status)
    assert rejected["status"] == "failed" and rejected["dispatch"] == "rejected"
    assert rejected["op_id"] == payload["op_id"] and rejected["error"]
    operation_path = "/operations/" + payload["op_id"]
    assert server.operator(operation_path) == rejected
    assert server.operator("/posts", payload, expected=409) == rejected
    server.operator("/posts", dict(payload, message="Changed rejected immutable request"), expected=409)
    assert server.operator(operation_path) == rejected
    assert server.native(posts_path) == before, "Rejected intent created a canonical post"


def exercise(server, ledger_path, run_id):
    backend = Backend(server.base + AGENT, server.coordinator)
    transport = FakeNative()
    ledger = Ledger(ledger_path)
    runtime = Coordinator(ledger, backend, transport)
    identities = [NativeIdentity("codex", new_id(), parent_known=True,
                                 target={"native_epoch": "operator-fixture"}, evidence={"fixture": True})
                  for _ in range(2)]
    a, b = identities
    child = NativeIdentity("codex", new_id(), a.session_id, True,
                           target={"native_epoch": "operator-fixture"}, evidence={"fixture": True})
    try:
        for label, identity in zip(("A", "B", "observer"), (a, b, child)):
            runtime.register(identity, "Murmur operator check " + label + " " + run_id)
        dm = runtime.mutate(a.actor, "conversation", kind="dm", members=[b.actor])["conversation"]["id"]
        channel = runtime.mutate(a.actor, "conversation", kind="channel", name="Operator check " + run_id,
                               members=[b.actor])["conversation"]["id"]
        runtime.sync(b.actor)
        ledger.set_mute(b.actor, channel, True)
        seed = runtime.mutate(a.actor, "post", channel_id=dm, message="Retained fixture work")["post"]
        foreign_root = runtime.mutate(a.actor, "post", channel_id=channel, message="Ignored fixture work")["post"]
        runtime.sync(b.actor)
        runtime.sync(child.actor)
        runtime.deliver(b)
        attempt = ledger.pending_attempts(b.actor)[0]
        ledger.admit(attempt["token"], b.actor, routing_binding(b), "operator-fixture-turn",
                     native_identity=asdict(b))
        ledger.mark_read(b.actor, [p["id"] for p in runtime.read(b.actor, dm)])
        runtime.set_saved(b.actor, seed["id"], True)
        users = [ledger.actor(identity.actor)["user_id"] for identity in (a, b, child)]

        # Both snapshots are deliberately populated, rather than empty-state checks.
        before = table_snapshot(ledger)
        native_before = native_snapshot(server, dm, users)
        assert {m["user_id"] for m in native_before["members"]} == set(users[:2])
        server.native("/channels/" + dm + "/members/" + server.user_id, expected=404)
        for _ in range(2):
            bootstrap = server.operator("/bootstrap")
            entry = next(c for c in bootstrap["conversations"] if c["id"] == dm)
            assert set(entry["members"]) == {a.actor, b.actor}
            assert child.actor in entry["observers"]
            assert [p["id"] for p in all_operator_posts(server, dm)] == [seed["id"]]
            assert server.operator("/threads/" + seed["id"])["posts"][0]["id"] == seed["id"]
        assert table_snapshot(ledger) == before, "Operator reads changed the private attention ledger"
        assert native_snapshot(server, dm, users) == native_before, "Operator reads changed native agent state"

        root_request = {"op_id": new_id(), "channel_id": dm, "message": "Human root " + run_id}
        root = server.operator("/posts", root_request)["post"]
        assert root["user_id"] == server.user_id and not root["root_id"]
        assert server.operator("/posts", root_request)["post"]["id"] == root["id"]
        server.operator("/posts", dict(root_request, message="Changed immutable request"), expected=409)
        reply_request = {"op_id": new_id(), "channel_id": dm, "root_id": root["id"],
                         "message": "Human threaded correction " + run_id}
        reply = server.operator("/posts", reply_request)["post"]
        assert reply["user_id"] == server.user_id and reply["root_id"] == root["id"]
        assert server.operator("/posts", reply_request)["post"]["id"] == reply["id"]
        operation = server.operator("/operations/" + root_request["op_id"])
        assert operation == {"status": "complete", "result": {"post_id": root["id"], "channel_id": dm}}
        for field, value in (("actor", a.actor), ("user_id", users[0]), ("causal", {"chain_ids": []})):
            server.operator("/posts", dict(root_request, **{field: value}), expected=400)
        rejected_send(server, dict(reply_request, op_id=new_id(), root_id=foreign_root["id"]), 400)
        rejected_send(server, dict(reply_request, op_id=new_id(), root_id=reply["id"]), 400)
        server.operator("/conversations/" + dm + "/posts?before=" + foreign_root["id"], expected=400)
        server.operator("/threads/" + reply["id"], expected=400)
        native_op = ledger.outbounds(a.actor)[0]["op_id"]
        server.operator("/operations/" + native_op, expected=403)
        server.operator("/posts", dict(root_request, op_id=native_op), expected=409)

        team = server.native("/channels/" + channel)["team_id"]
        unmanaged = server.native("/channels", {"team_id": team, "type": "P", "name": "op-unmanaged-" + run_id,
                                                "display_name": "Unmanaged operator fixture"}, expected=201)
        unmanaged_post = server.native("/posts", {"channel_id": unmanaged["id"], "message": "Outside managed scope"}, expected=201)
        server.operator("/conversations/" + unmanaged["id"] + "/posts", expected=404)
        server.operator("/threads/" + unmanaged_post["id"], expected=404)
        invalid = dict(root_request, op_id=new_id(), channel_id=unmanaged["id"])
        rejected_send(server, invalid, 404)

        runtime.sync(b.actor)
        unread = {p["id"]: p for p in ledger.unread_posts(b.actor)}
        assert {root["id"], reply["id"], foreign_root["id"]} == set(unread)
        assert unread[root["id"]]["announced_at"] is None
        assert [p["id"] for p in runtime.read(b.actor, dm, root=root["id"], limit=1)] == [root["id"]]
        ledger.mark_read(b.actor, [root["id"]])
        bot_reply = runtime.mutate(b.actor, "post", channel_id=dm, root_id=root["id"],
                                   message="Synthetic fixture reply " + run_id)["post"]
        assert bot_reply["user_id"] == users[1]
        assert [p["id"] for p in server.operator("/threads/" + root["id"])["posts"]] == [root["id"], reply["id"], bot_reply["id"]]

        lost_request = dict(root_request, op_id=new_id(), message="Discarded response " + run_id)
        recovered = discarded_response(server, lost_request)
        echo = server.operator("/posts", lost_request)["post"]
        assert echo["id"] == recovered["result"]["post_id"] and echo["user_id"] == server.user_id
        items = all_operator_posts(server, dm)
        assert len(items) == 5 and sum(p["id"] == echo["id"] for p in items) == 1
        assert [p["id"] for p in backend.posts(b.actor, dm)["posts"]] == [p["id"] for p in items]
        assert {m["user_id"] for m in server.native("/channels/" + dm + "/members")} == set(users[:2])
        server.native("/channels/" + dm + "/members/" + server.user_id, expected=404)

        stable = table_snapshot(ledger)
        ledger.close()
        ledger = Ledger(ledger_path)
        assert table_snapshot(ledger) == stable, "Private ledger restart changed persisted attention state"
        assert server.operator("/operations/" + lost_request["op_id"]) == recovered
        return {"actors": [identity.actor for identity in (a, b, child)], "dm": dm,
                "human_root": root["id"], "human_reply": reply["id"], "fixture_reply": bot_reply["id"],
                "discarded_response_operation": lost_request["op_id"], "fake_transport_calls": len(transport.calls)}
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Create fresh fixtures after the reviewed backend deployment")
    parser.add_argument("--coordinator-config", type=Path, help="Optional expected fixture config path; cannot select a server")
    parser.add_argument("--admin-session", type=Path, help="Optional expected fixture admin path; cannot select a server")
    fixture_arguments(parser)
    args = parser.parse_args()
    if not args.run:
        parser.error("Use --run only after the coordinator approves the reviewed deployment")
    fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
    if (args.coordinator_config and args.coordinator_config.resolve() != fixture.state / "coordinator.json"
            or args.admin_session and args.admin_session.resolve() != fixture.state / "admin-session.json"):
        parser.error("Explicit credential paths must match the marked disposable fixture")
    server = Server(fixture.coordinator, fixture.admin)
    # Confirm the operator contract before creating any fixture identities.
    bootstrap = server.operator("/bootstrap")
    assert bootstrap["user"]["id"] == server.user_id
    # One overlapping read sample, not a load test or a timing pass threshold.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(server.operator, "/bootstrap")
        started = time.monotonic()
        server.call(AGENT + "/health", token=server.coordinator)
        coordinator_seconds = time.monotonic() - started
        pending.result()
    run_id = uuid.uuid4().hex[:10]
    check_auth(server, run_id)
    with tempfile.TemporaryDirectory(prefix="murmur-operator-http-") as directory:
        result = exercise(server, Path(directory) / "operations.sqlite3", run_id)
    print(json.dumps({"status": "pass", "run": run_id, **result,
                      "initial_managed_conversations": len(bootstrap["conversations"]),
                      "max_bootstrap_seconds": round(max(server.bootstrap_seconds), 3),
                      "concurrent_coordinator_get_seconds": round(coordinator_seconds, 3),
                      "scope": "HTTP fixtures/private ledger/fake transport; no native live proof or plugin restart"}, indent=2))


if __name__ == "__main__":
    main()
