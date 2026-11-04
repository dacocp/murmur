#!/usr/bin/env python3
"""Small real-Mattermost Saved Posts interop/permission regression.

Run after deploying the updated plugin. Native preference APIs use only fresh
server-side fixture bots; no native-agent enrollment or wakeup is performed.
--restart also restarts the plugin and requires the coordinator to be held idle.
"""

import argparse
import time
from urllib.parse import urlencode

from integration_test import (API, agent, agent_info, call, configure_fixture, conversation, conversations,
                              feed, fixture_arguments, op, post)


def starred(actor, post_id, value=True):
    result = call(API + "/saved/" + post_id, {"actor": actor, "starred": value})
    assert result == {"post_id": post_id, "starred": value}, result


def saved(actor):
    return call(API + "/saved?" + urlencode({"actor": actor}))["saved"]


def native_preferences(user_id):
    preferences = call("/api/v4/users/" + user_id + "/preferences", admin=True)
    # Mattermost's native endpoint serializes an untouched preference slice as
    # JSON null. Murmur's public saved endpoint always returns an array.
    return [] if preferences is None else preferences


def native_saved_ids(user_id):
    return {p["name"] for p in native_preferences(user_id)
            if p["category"] == "flagged_post" and p["value"] == "true"}


def set_native(user_id, category, name, value):
    call("/api/v4/users/" + user_id + "/preferences", [{
        "user_id": user_id, "category": category, "name": name, "value": value,
    }], admin=True, method="PUT")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true", help="Requires coordinator hold: disable/re-enable only Murmur")
    fixture_arguments(parser)
    args = parser.parse_args()
    configure_fixture(args)
    parent, peer, outsider = agent("saved parent"), agent("saved peer"), agent("saved outsider")
    channel = conversation(parent, "channel", [peer], "Saved Posts review")
    dm = conversation(parent, "dm", [peer])
    root = post(parent, channel, "Historical root to save")
    reply = post(peer, channel, "Save this exact reply", root["id"])
    private = post(parent, dm, "Inherited private context")
    starred(parent, root["id"])
    child = agent("saved child", parent)
    assert saved(child) == [], "fork copied parent's native preferences"
    own_dm = conversation(child, "dm", [peer])
    own = post(child, own_dm, "My own message can be saved")
    child_user = agent_info(child)["user_id"]
    outsider_user = agent_info(outsider)["user_id"]
    # Native UI/REST and Murmur share one preference authority. A false entry
    # is not listed; unrelated settings survive every mutation.
    set_native(child_user, "display_settings", "use_military_time", "true")
    set_native(child_user, "flagged_post", root["id"], "false")
    set_native(child_user, "flagged_post", reply["id"], "true")
    assert [row["post_id"] for row in saved(child)] == [reply["id"]]
    baseline = feed(child)
    for post_id in (root["id"], private["id"], own["id"], root["id"]):
        starred(child, post_id)
    ids = {root["id"], reply["id"], private["id"], own["id"]}
    assert native_saved_ids(child_user) == ids
    rows = saved(child)
    assert [row["post_id"] for row in rows] == sorted(ids)
    by_id = {row["post_id"]: row["post"] for row in rows}
    assert by_id[reply["id"]]["root_id"] == root["id"]
    assert by_id[own["id"]]["user_id"] == child_user
    # Observer saving must not become a native membership or post permission.
    call("/api/v4/channels/" + dm + "/members/" + child_user, admin=True, expected=404)
    call(API + "/posts", {"actor": child, "op_id": op(), "channel_id": dm, "message": "still read-only"}, expected=403)
    call(API + "/saved/" + private["id"], {"actor": outsider, "starred": True}, expected=403)
    assert native_saved_ids(outsider_user) == set()
    quiet = feed(child, baseline["cursor"], baseline["generation"])
    assert quiet["changes"] == [] and quiet["generation"] == baseline["generation"] and quiet["reset"] is False
    # A public-channel leaver remains a quiet history visitor. Saved-only public
    # access must not create membership or new arrivals.
    call(API + "/conversations/" + channel + "/leave", {"actor": parent, "op_id": op()})
    public_saved = saved(parent)
    assert len(public_saved) == 1 and public_saved[0]["post"]["id"] == root["id"]
    assert public_saved[0]["conversation"]["role"] is None
    outsider_before = feed(outsider)
    starred(outsider, root["id"])
    public_visitor = saved(outsider)
    assert len(public_visitor) == 1 and public_visitor[0]["post"]["id"] == root["id"]
    assert public_visitor[0]["conversation"]["role"] is None
    assert channel not in conversations(outsider)
    call("/api/v4/channels/" + channel + "/members/" + outsider_user, admin=True, expected=404)
    assert feed(outsider, outsider_before["cursor"], outsider_before["generation"])["changes"] == []
    starred(outsider, root["id"], False)
    starred(parent, root["id"], False)
    starred(parent, root["id"], False)
    assert saved(parent) == []
    # Private DM hiding actually revokes this parent's Murmur access. Its
    # native preference remains opaque and removable; descendant grants persist.
    starred(parent, private["id"])
    call(API + "/conversations/" + dm + "/leave", {"actor": parent, "op_id": op()})
    assert saved(parent) == [{"post_id": private["id"], "unavailable": True}]
    starred(parent, private["id"], False)
    starred(parent, private["id"], False)
    assert saved(parent) == []
    assert root["id"] in native_saved_ids(child_user)
    assert private["id"] in {row["post_id"] for row in saved(child) if not row.get("unavailable")}
    set_native(outsider_user, "flagged_post", private["id"], "true")
    assert saved(outsider) == [{"post_id": private["id"], "unavailable": True}]
    starred(outsider, private["id"], False)
    assert saved(outsider) == []
    for invalid in ({"actor": child}, {"actor": child, "starred": None},
                    {"actor": child, "starred": "false"}, {"actor": child, "starred": True, "user_id": outsider_user}):
        call(API + "/saved/" + root["id"], invalid, expected=400)
    call(API + "/saved/not-a-post-id", {"actor": child, "starred": False}, expected=400)
    # Normal native deletion clears its flagged_post preference for all users.
    # No custom tombstone or bookmark cleanup job is involved.
    call("/api/v4/posts/" + reply["id"], admin=True, method="DELETE")
    deadline = time.monotonic() + 10
    while reply["id"] in native_saved_ids(child_user):
        assert time.monotonic() < deadline, "native message deletion did not clear flagged_post"
        time.sleep(0.1)
    assert reply["id"] not in {row["post_id"] for row in saved(child)}
    unrelated = {(p["category"], p["name"]): p["value"] for p in native_preferences(child_user)}
    assert unrelated[("display_settings", "use_military_time")] == "true"
    grandchild = agent("saved grandchild", child)
    assert saved(grandchild) == []
    if args.restart:
        expected_ids = {row["post_id"] for row in saved(child)}
        call("/api/v4/plugins/murmur/disable", {}, admin=True)
        try:
            # An additional native save while the plugin is absent proves that
            # restart reads the canonical preferences rather than a local copy.
            set_native(outsider_user, "flagged_post", private["id"], "true")
        finally:
            call("/api/v4/plugins/murmur/enable", {}, admin=True)
        assert {row["post_id"] for row in saved(child)} == expected_ids
        assert saved(outsider) == [{"post_id": private["id"], "unavailable": True}]
        starred(outsider, private["id"], False)
    print("PASS: native Saved Posts interop, fork isolation, observer ACLs, own/history/reply saves, desired-state retries, quiet feed and deletion cleanup" +
          (", plus plugin restart" if args.restart else ""))


if __name__ == "__main__":
    main()
