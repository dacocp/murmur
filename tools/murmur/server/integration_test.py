#!/usr/bin/env python3
"""Exercise a real, already-bootstrapped dedicated Mattermost installation.

Creates fresh server-only fixture identities, never native-agent enrollment.
The restart case briefly disables only the Murmur plugin. Do not run while a
native live-test turn is in progress.
"""

import argparse
import http.client
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from server.fixture import fixture_arguments, load_fixture
from murmur.protocol import ATTENTION_VERSION

BASE = None
API = "/plugins/murmur/v1"
CONFIG = ADMIN = FIXTURE = None
AGENTS = {}
FIXTURE_PARENTS = {}


def configure_fixture(args):
    global BASE, CONFIG, ADMIN, FIXTURE
    FIXTURE = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
    BASE, CONFIG, ADMIN = FIXTURE.origin, FIXTURE.coordinator, FIXTURE.admin
    return FIXTURE


def agent_info(actor):
    return AGENTS[actor]


def call(path, data=None, *, admin=False, expected=200, method=None):
    if FIXTURE is None:
        raise ValueError("Select a marked disposable fixture before making requests")
    statuses = expected if isinstance(expected, tuple) else (expected,)
    token = ADMIN["token"] if admin else CONFIG["token"]
    req = Request(BASE + path, data=None if data is None else json.dumps(data).encode(),
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method=method)
    try:
        with urlopen(req, timeout=60) as response:
            result = json.load(response)
            assert response.status in statuses, (path, response.status, expected)
            return result
    except HTTPError as exc:
        result = json.load(exc)
        if exc.code not in statuses:
            raise AssertionError((path, exc.code, expected, result)) from exc
        return result


def op():
    return str(uuid.uuid4())


def agent(name, parent=None):
    actor = "codex:" + op()
    # These synthetic fixtures explicitly declare their parent facts; real
    # native enrollment obtains these facts from its certified adapter.
    edges = [{"actor": actor, "parent": parent}]
    ancestor = parent
    while ancestor in FIXTURE_PARENTS:
        edges.append({"actor": ancestor, "parent": FIXTURE_PARENTS[ancestor]})
        ancestor = FIXTURE_PARENTS[ancestor]
    request = {"actor": actor, "name": "Murmur integration " + name, "mode": "native",
               "lineage": edges, "enrollment_id": op()}
    if parent:
        request["parent"] = parent
    result = call(API + "/register", request)["agent"]
    assert result["id"] == actor
    AGENTS[actor] = result
    FIXTURE_PARENTS[actor] = parent
    return actor


def conversations(actor):
    return {c["id"]: c for c in call(API + "/conversations?" + urlencode({"actor": actor}))["conversations"]}


def conversation(actor, kind, peers, name="Build review"):
    return call(API + "/conversations", {"actor": actor, "op_id": op(), "kind": kind, "name": name,
                                        "members": peers})["conversation"]["id"]


def post(actor, channel, message, root=None, operation=None):
    data = {"actor": actor, "channel_id": channel, "message": message, "op_id": operation or op()}
    if root:
        data["root_id"] = root
    return call(API + "/posts", data)["post"]


def feed(actor, cursor=0, generation=None, limit=1000):
    query = {"actor": actor, "cursor": cursor, "limit": limit, "attention_version": ATTENTION_VERSION}
    if generation is not None:
        query["generation"] = generation
    return call(API + "/changes?" + urlencode(query))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    configure_fixture(parser.parse_args())
    assert call(API + "/health")["version"] == "0.1.0"
    # Read the exact runtime version, independently of Compose's requested tag.
    version = subprocess.check_output(FIXTURE.compose + ["exec", "-T", "mattermost", "bin/mattermost", "version"],
                                      cwd=FIXTURE.state, text=True)
    assert "12.0.1" in version, version
    parent, peer, outsider = agent("parent"), agent("peer"), agent("outsider")
    channel = conversation(parent, "channel", [peer])
    dm = conversation(parent, "dm", [peer])
    root = post(parent, channel, "Root before fork")
    private = post(parent, dm, "Parent DM stays in parent branch")
    child = agent("child", parent)
    grandchild = agent("grandchild", child)
    later = conversation(parent, "channel", [peer], "API work")
    for observer in (child, grandchild):
        rooms = conversations(observer)
        assert all(rooms[c]["role"] == "observer" for c in (channel, dm, later))
        call(API + "/posts", {"actor": observer, "op_id": op(), "channel_id": channel, "message": "forbidden"}, expected=403)
    call(API + "/posts/" + private["id"] + "?" + urlencode({"actor": outsider}), expected=403)
    call(API + "/threads/" + private["id"] + "?" + urlencode({"actor": outsider}), expected=403)
    public = call(API + "/threads/" + root["id"] + "?" + urlencode({"actor": outsider}))
    assert [item["id"] for item in public["posts"]] == [root["id"]]
    assert channel not in conversations(outsider), "public preview granted participation"
    assert feed(outsider)["changes"] == [], "public preview created arrivals"
    call(API + "/posts", {"actor": outsider, "op_id": op(), "channel_id": channel,
                          "message": "read access is not participation"}, expected=403)
    call(API + "/conversations/" + dm + "/join", {"actor": child, "op_id": op()}, expected=403)
    call(API + "/conversations/" + channel + "/join", {"actor": child, "op_id": op()})
    reply = post(child, channel, "A real child reply", root["id"])
    assert reply["root_id"] == root["id"] and reply["user_id"] != root["user_id"]
    call(API + "/threads/" + reply["id"] + "?" + urlencode({"actor": child}), expected=400)
    thread = call(API + "/threads/" + root["id"] + "?" + urlencode({"actor": grandchild}))["posts"]
    assert [item["id"] for item in thread] == [root["id"], reply["id"]]
    fresh_dm = conversation(child, "dm", [peer])
    assert fresh_dm != dm
    assert call(API + "/conversations/" + fresh_dm + "/posts?" + urlencode({"actor": child}))["posts"] == []
    call(API + "/posts", {"actor": child, "op_id": op(), "channel_id": fresh_dm,
                           "root_id": root["id"], "message": "wrong channel"}, expected=400)
    leave = op()
    call(API + "/conversations/" + channel + "/leave", {"actor": parent, "op_id": leave})
    assert channel in conversations(grandchild) and channel not in conversations(parent)
    call(API + "/conversations/" + channel + "/leave", {"actor": parent, "op_id": leave})
    call(API + "/conversations/" + channel + "/join", {"actor": parent, "op_id": leave}, expected=409)
    call(API + "/conversations/" + channel + "/join", {"actor": parent, "op_id": op()})
    # Native history exceeds one server page and contains real threaded posts.
    for i in range(105):
        post(peer, channel, "Canonical paging " + str(i))
    page = call(API + "/conversations/" + channel + "/posts?" + urlencode({"actor": child, "limit": 20}))
    assert len(page["posts"]) == 20 and page["next_before"]
    older = call(API + "/conversations/" + channel + "/posts?" + urlencode({"actor": child, "limit": 1000, "before": page["next_before"]}))
    assert len(older["posts"]) == 87, len(older["posts"])
    reference = call(API + "/conversations/" + channel + "/posts?" + urlencode({"actor": grandchild, "limit": 1000}))["posts"]
    assert len(reference) == len({item["id"] for item in reference}) == 107
    assert {root["id"], reply["id"]} <= {item["id"] for item in reference}
    assert call(API + "/posts/" + private["id"] + "?" + urlencode({"actor": grandchild}))["post"]["id"] == private["id"]
    known = feed(grandchild)
    assert known["changes"] == [], "inherited canonical history became observer arrivals"
    empty = feed(grandchild, known["cursor"], known["generation"])
    assert empty["changes"] == [] and empty["reset"] is False
    # The HTTP response is deliberately discarded. A repeat binds to the same
    # durable op and must return the single canonical post, never create again.
    lost_id = op()
    payload = {"actor": peer, "op_id": lost_id, "channel_id": channel, "message": "Discard this HTTP response"}
    endpoint = urlsplit(BASE)
    transport = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
    connection = transport(endpoint.hostname, endpoint.port, timeout=30)
    connection.request("POST", API + "/posts", json.dumps(payload), {
        "Authorization": "Bearer " + CONFIG["token"], "Content-Type": "application/json",
    })
    try:
        # Keep the TCP request alive until a separate read proves the durable
        # native effect. We never consume the original response. Closing before
        # admission would test an aborted HTTP request rather than response loss.
        for _ in range(100):
            result = call(API + "/operations/" + lost_id + "?" + urlencode({"actor": peer}), expected=(200, 404))
            if result.get("status") == "complete":
                break
            time.sleep(0.1)
        assert result.get("status") == "complete", result
    finally:
        connection.close()
    replay = call(API + "/posts", payload)["post"]
    assert replay["id"] == result["result"]["post_id"]
    all_posts = call(API + "/conversations/" + channel + "/posts?" + urlencode({"actor": peer, "limit": 1000}))["posts"]
    assert sum(p["message"] == payload["message"] for p in all_posts) == 1
    call(API + "/conversations/" + channel + "/leave", {"actor": peer, "op_id": op()})
    departed = call(API + "/operations/" + lost_id + "?" + urlencode({"actor": peer}))
    assert departed == {"status": "complete", "result": {"post_id": replay["id"], "channel_id": channel}}
    call(API + "/operations/" + lost_id + "?" + urlencode({"actor": outsider}), expected=403)
    assert call(API + "/posts/" + replay["id"] + "?" + urlencode({"actor": peer}))["post"]["id"] == replay["id"]
    # Disable the plugin, create a canonical native post, then re-enable. No
    # plugin callback can have observed this post; recovery must discover it.
    member_known = feed(parent)
    call("/api/v4/plugins/murmur/disable", {}, admin=True)
    try:
        missed = call("/api/v4/posts", {"channel_id": channel, "message": "Posted while Murmur was unloaded"}, admin=True, expected=201)
    finally:
        call("/api/v4/plugins/murmur/enable", {}, admin=True)
    recovered = feed(parent, member_known["cursor"], member_known["generation"])
    assert missed["id"] in {item["post"]["id"] for item in recovered["changes"]}
    observer = feed(grandchild, known["cursor"], known["generation"])
    assert observer["changes"] == [], "restart repair created observer arrivals"
    assert call(API + "/posts/" + missed["id"] + "?" + urlencode({"actor": grandchild}))["post"]["id"] == missed["id"]
    assert channel in conversations(grandchild)
    abandoned = conversation(parent, "channel", [peer], "Persistent observer history")
    retained = post(parent, abandoned, "Keep after all original members leave")
    for member in (parent, peer):
        call(API + "/conversations/" + abandoned + "/leave", {"actor": member, "op_id": op()})
    assert abandoned in conversations(grandchild)
    assert call(API + "/posts/" + retained["id"] + "?" + urlencode({"actor": grandchild}))["post"]["id"] == retained["id"]
    # A system administrator can preserve create_at through the normal native
    # API. Put more than one canonical SQL page at the exact same millisecond;
    # the native Before endpoint's strict timestamp boundary would omit rows.
    tie_actor = agent("equal-ms")
    tie_channel = conversation(tie_actor, "channel", [outsider], "Equal millisecond recovery")
    timestamp = int(time.time() * 1000) - 1000
    def create_equal(i):
        item = call("/api/v4/posts?set_online=false", {
            "channel_id": tie_channel, "message": "Equal millisecond " + str(i), "create_at": timestamp,
        }, admin=True, expected=201)
        assert item["create_at"] == timestamp
        return item["id"]
    # Suppress every plugin hint so the exact-set assertion proves canonical
    # startup repair across SQL pages, independent of callback scheduling.
    call("/api/v4/plugins/murmur/disable", {}, admin=True)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            equal_ids = set(pool.map(create_equal, range(1030)))
    finally:
        call("/api/v4/plugins/murmur/enable", {}, admin=True)
    assert len(equal_ids) == 1030
    first = feed(outsider, limit=1000)
    second = feed(outsider, first["cursor"], first["generation"], limit=1000)
    observed_ids = [item["post"]["id"] for item in first["changes"] + second["changes"]]
    assert first["more"] is True and second["more"] is False
    assert len(observed_ids) == len(set(observed_ids)) == 1030
    assert set(observed_ids) == equal_ids
    print("PASS: real Mattermost 12.0.1/Postgres 16 identities, inheritance, private ACLs, threads, joins/leaves, paging, lost HTTP response, restart/backfill and 1030 equal-ms posts")


if __name__ == "__main__":
    main()
