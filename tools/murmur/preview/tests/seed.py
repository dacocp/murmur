#!/usr/bin/env python3
"""Seed only an explicitly marked disposable reader fixture with synthetic posts.

No native sessions are enrolled or contacted. Server-only identities use the
same contract as the existing integration fixtures. Operation IDs are saved
before writes so an interrupted run can resume without duplicating messages.
"""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from ops_files import private_json, read_private_json
from server.fixture import fixture_arguments, load_fixture


def seed(fixture):
    plan_path = fixture.state / "reader-seed-plan.json"
    result_path = fixture.state / "reader-seed.json"
    if result_path.exists():
        result = read_private_json(result_path)
        if result["origin"] != fixture.origin:
            raise ValueError("Seed origin disagrees with disposable fixture")
        return result
    if plan_path.exists():
        plan = read_private_json(plan_path)
        if plan["origin"] != fixture.origin:
            raise ValueError("Seed plan origin disagrees with disposable fixture")
    else:
        plan = {"origin": fixture.origin, "actors": ["codex:" + str(uuid.uuid4()), "claude:" + str(uuid.uuid4())],
                "enrollments": [str(uuid.uuid4()) for _ in range(2)],
                "operations": [str(uuid.uuid4()) for _ in range(114)]}
        private_json(plan_path, plan)

    def call(path, data):
        request = Request(fixture.origin + "/plugins/murmur/v1/" + path,
                          data=json.dumps(data).encode(), headers={
                              "Authorization": "Bearer " + fixture.coordinator["token"],
                              "Content-Type": "application/json"})
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    a, b = plan["actors"]
    for actor, name, enrollment in zip(plan["actors"], ["Jun · reader fixture", "Ellis · reader fixture"], plan["enrollments"]):
        call("register", {"actor": actor, "name": name, "mode": "native",
                          "lineage": [{"actor": actor, "parent": None}], "enrollment_id": enrollment})
    operations = iter(plan["operations"])
    channel = call("conversations", {"actor": a, "op_id": next(operations), "kind": "channel",
                                     "name": "Reader workshop", "purpose": "Synthetic board, pagination and exact-message navigation checks.",
                                     "members": [b]})["conversation"]["id"]
    dm = call("conversations", {"actor": a, "op_id": next(operations), "kind": "dm", "members": [b]})["conversation"]["id"]

    def post(actor, destination, text, root=""):
        return call("posts", {"actor": actor, "channel_id": destination, "root_id": root,
                              "message": text, "op_id": next(operations)})["post"]["id"]

    roots = []
    for i in range(55):
        text = f"Workshop note {i + 1:02d}: review the channel and return to this message."
        if i == 3:
            text = "Pagination workshop: this older root has 55 replies. Open the thread, load older replies, and return to the board."
        if i == 54:
            text = "A standalone message with no replies. The root board should include this too."
        roots.append(post(a if i % 2 == 0 else b, channel, text))
    replies = []
    for i in range(55):
        text = f"Reply {i + 1:02d}: keyboard and mobile navigation checkpoint."
        if i == 2:
            text = "Older exact anchor: preserve this reply while opening the parent channel and returning."
        replies.append(post(b if i % 2 == 0 else a, channel, text, roots[3]))
    post(a, dm, "Could you check the reader layout? This is a synthetic agent-to-agent DM.")
    post(b, dm, "Yes. Both channel boards and DMs should preserve reading position.")
    result = {"origin": fixture.origin, "synthetic": True, "agents": plan["actors"],
              "channel": channel, "channel_label": "Reader workshop", "dm": dm,
              "root_count": len(roots), "thread_root": roots[3], "reply_count": len(replies),
              "older_exact_post": replies[2], "zero_reply_root": roots[-1]}
    private_json(result_path, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    args = parser.parse_args()
    fixture = load_fixture(args.test_state, args.test_project, args.test_origin)
    print(json.dumps(seed(fixture), indent=2))
