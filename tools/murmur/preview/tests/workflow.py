#!/usr/bin/env python3
"""Explicit incoming events on a marked fixture, using already seeded actors.

No registration, native session enrollment, wakeup or automatic arrival loop.
Named events keep frozen operation IDs before writes; retry the same event
after an interrupted request instead of inventing another message operation.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from hosting import Client, OPERATOR, proof_origin
from ops_files import private_directory, private_json, read_private_json
from server.fixture import fixture_arguments, load_fixture


def operator(client, path, data=None, *, method=None, expected=200):
    result, headers = client.json(OPERATOR + path, data, method=method, expected=expected)
    cache = headers.get("Cache-Control", "").lower()
    if "private" not in cache or "no-store" not in cache:
        raise AssertionError("Operator response lacks private,no-store")
    return result


def arrive(fixture, event, kind, *, root="", channel="", actor=None, text=None, human=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", event):
        raise ValueError("Event name must contain 1–100 letters, digits, underscores or hyphens")
    if kind not in {"routine", "mention", "direct"} or kind == "routine" and not root:
        raise ValueError("Choose routine with a root, mention, or a fresh direct message")
    if kind == "direct" and (root or channel):
        raise ValueError("A direct-contact event opens the selected human/agent DM and starts a fresh root")
    seed = read_private_json(fixture.state / "reader-seed.json")
    if seed.get("origin") != fixture.origin or not seed.get("synthetic") or not seed.get("agents"):
        raise ValueError("Expected this fixture's existing synthetic actor seed")
    admin = human or Client(proof_origin(fixture), fixture.admin["token"])
    if admin.origin != proof_origin(fixture):
        raise ValueError("Human client must use the selected fixture origin")
    if not admin.user_id:
        admin.user_id = fixture.admin["user_id"]
    coordinator = Client(proof_origin(fixture), fixture.coordinator["token"])
    boot = operator(admin, "/bootstrap")
    if boot["user"]["id"] != admin.user_id:
        raise ValueError("Fixture human identity changed")
    if actor is not None and actor not in seed["agents"]:
        raise ValueError("Only this fixture's existing synthetic actors may publish an arrival")
    if text is None:
        text = ("@" + boot["user"]["username"] + " " if kind == "mention" else "") + {
            "routine": "Synthetic routine thread reply", "mention": "Synthetic explicit personal mention",
            "direct": "Synthetic fresh direct contact",
        }[kind] + " [fixture event " + event + "]"
    if not text.strip() or len(text) > 16000:
        raise ValueError("Fixture message must contain 1–16000 characters")
    requested = {"kind": kind, "root": root, "channel": channel, "actor": actor,
                 "text": text, "human_id": admin.user_id}
    directory = private_directory(fixture.state / "workflow-events")
    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / (event + ".json")
        if path.exists():
            plan = read_private_json(path)
            if plan.get("origin") != fixture.origin or plan.get("request") != requested:
                raise ValueError("Event is already bound to another immutable fixture request")
            if plan.get("receipt"):
                return plan["receipt"]
        else:
            selected = actor or seed["agents"][0]
            target = channel
            if kind != "direct":
                if root:
                    context = operator(admin, "/context/" + root)
                    if context["root"]["id"] != root or target and target != context["channel_id"]:
                        raise ValueError("Arrival root must be the original message in the selected conversation")
                    target = context["channel_id"]
                target = target or seed["channel"]
                conversation = next((item for item in boot["conversations"] if item["id"] == target), None)
                eligible = [candidate for candidate in seed["agents"] if candidate in (conversation or {}).get("members", [])]
                if not eligible or actor and actor not in eligible:
                    raise ValueError("The selected synthetic sender is not a member of this fixture conversation")
                selected = actor or eligible[0]
            plan = {"origin": fixture.origin, "request": requested, "actor": selected, "channel_id": target,
                    "post_op": str(uuid.uuid4()), "dm_op": str(uuid.uuid4())}
            private_json(path, plan)
        if kind == "direct" and not plan["channel_id"]:
            result = operator(admin, "/dms", {"agent_id": plan["actor"], "op_id": plan["dm_op"]})
            plan["channel_id"] = result["conversation"]["id"]
            private_json(path, plan)
        result, _ = coordinator.json("/plugins/murmur/v1/posts", {
            "actor": plan["actor"], "channel_id": plan["channel_id"], "root_id": root,
            "message": text, "op_id": plan["post_op"],
        })
        post = result["post"]
        if post["channel_id"] != plan["channel_id"] or (post.get("root_id") or "") != root:
            raise AssertionError("Incoming fixture post resolved to another destination")
        plan["receipt"] = {"event": event, "synthetic": True, "kind": kind, "post_id": post["id"],
                           "channel_id": plan["channel_id"], "root_id": root or post["id"], "op_id": plan["post_op"]}
        private_json(path, plan)
        return plan["receipt"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    parser.add_argument("--event", required=True, help="Stable event name; use the same name to recover an interrupted invocation")
    parser.add_argument("--kind", choices=("routine", "mention", "direct"), required=True)
    parser.add_argument("--root", default="")
    parser.add_argument("--channel", default="")
    parser.add_argument("--actor", help="One of reader-seed.json's existing synthetic actor IDs")
    parser.add_argument("--text", help="Synthetic message text; default identifies the named fixture event")
    args = parser.parse_args()
    try:
        fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
        result = arrive(fixture, args.event, args.kind, root=args.root, channel=args.channel,
                        actor=args.actor, text=args.text)
    except (OSError, ValueError, AssertionError) as error:
        parser.exit(1, str(error) + "; retain --event " + args.event + " for recovery\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
