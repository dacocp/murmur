#!/usr/bin/env python3
"""Personal-state/Inbox/send HTTP acceptance on a marked disposable fixture.

Uses two fresh test-human administrators and an isolated managed channel. Keeps
the demo human's credentials, native preferences and original seeded history
unchanged. All created IDs/operations are retained in a private named-run plan;
never erase or restore the fixture to rerun a failed check. No native agent sessions
are enrolled. Run only after the owner freezes/installs the workspace API.
"""

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlencode, urlsplit
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from hosting import Client, OPERATOR, proof_origin
from ops_files import private_directory, private_json, read_private_json
from server.fixture import fixture_arguments, load_fixture
from workflow import arrive, operator


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def all_posts(client, channel):
    posts, seen, cursor = {}, set(), None
    while True:
        page = operator(client, "/conversations/" + channel + "/posts?" + urlencode({
            "limit": 100, **({"before": cursor} if cursor else {})}))
        for post in page["posts"]:
            if post["id"] in posts:
                raise AssertionError("Canonical history pagination duplicated an existing post")
            posts[post["id"]] = post
        cursor = page.get("next_before")
        if not cursor:
            return posts
        if cursor in seen:
            raise AssertionError("Canonical history cursor repeated")
        seen.add(cursor)


def protected_snapshot(fixture):
    client = Client(proof_origin(fixture), fixture.admin["token"])
    client.user_id = fixture.admin["user_id"]
    seed = read_private_json(fixture.state / "reader-seed.json")
    boot = operator(client, "/bootstrap")
    old_ids = {seed["channel"], seed["dm"]}
    preferences, _ = client.json("/api/v4/users/" + client.user_id + "/preferences")
    result = {
        "files": {name: hashlib.sha256((fixture.state / name).read_bytes()).hexdigest() for name in (
            "credentials.json", "admin-session.json", "coordinator.json", "reader-seed.json", "reader-seed-plan.json",
            "test-installation.json", "compose.json", ".env")},
        "native_preferences": sorted(preferences or [], key=lambda row: json.dumps(row, sort_keys=True)),
        "conversations": sorted([chat for chat in boot["conversations"] if chat["id"] in old_ids], key=lambda chat: chat["id"]),
        "history": {channel: all_posts(client, channel) for channel in sorted(old_ids)},
    }
    return result


class Run:
    def __init__(self, fixture, name):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}", name):
            raise ValueError("Run name must contain 1–70 letters, digits, underscores or hyphens")
        self.fixture, self.name = fixture, name
        self.home = private_directory(private_directory(fixture.state / "workspace-acceptance") / name)
        self.path = self.home / "plan.json"
        if self.path.exists():
            self.plan = read_private_json(self.path)
            if self.plan.get("origin") != fixture.origin:
                raise ValueError("Existing acceptance run belongs to another fixture origin")
        else:
            suffix = uuid.uuid4().hex[:10]
            self.plan = {"origin": fixture.origin, "operations": {}, "humans": [
                {"username": "murmur-work-" + suffix + "-" + label,
                 "password": "Fixture-" + secrets.token_urlsafe(24) + "aA1!"} for label in ("a", "b")
            ]}
            self.save()
        self.admin = Client(proof_origin(fixture), fixture.admin["token"])
        self.admin.user_id = fixture.admin["user_id"]
        self.agent = Client(proof_origin(fixture), fixture.coordinator["token"])
        self.seed = read_private_json(fixture.state / "reader-seed.json")
        self.sessions, self.checks = [], []

    def save(self):
        private_json(self.path, self.plan)

    def operation(self, name):
        if name not in self.plan["operations"]:
            self.plan["operations"][name] = str(uuid.uuid4())
            self.save()
        return self.plan["operations"][name]

    def human(self, index):
        account = self.plan["humans"][index]
        if not re.fullmatch(r"[a-z0-9]{26}", account.get("id", "")):
            user, _ = self.admin.json("/api/v4/users/username/" + account["username"], expected=(200, 404))
            if not user or not re.fullmatch(r"[a-z0-9]{26}", user.get("id", "")):
                user, _ = self.admin.json("/api/v4/users", {"username": account["username"],
                    "email": account["username"] + "@example.invalid", "password": account["password"]}, expected=201)
            account["id"] = user["id"]
            self.save()
        current, _ = self.admin.json("/api/v4/users/" + account["id"])
        if current["username"] != account["username"]:
            raise AssertionError("Acceptance account ID no longer matches its frozen username")
        if "system_admin" not in current["roles"].split():
            self.admin.call("/api/v4/users/" + account["id"] + "/roles", {"roles": "system_user system_admin"}, method="PUT")
        client = Client(proof_origin(self.fixture))
        user, headers = client.json("/api/v4/users/login", {"login_id": account["username"], "password": account["password"]})
        if user["id"] != account["id"] or not headers.get("Token"):
            raise AssertionError("Dedicated acceptance human identity mismatch")
        client.user_id, client.acceptance_token = user["id"], headers["Token"]
        self.sessions.append(client)
        return client

    def channel(self):
        result, _ = self.agent.json("/plugins/murmur/v1/conversations", {
            "actor": self.seed["agents"][0], "members": [self.seed["agents"][1]], "kind": "channel",
            "name": "Workspace acceptance " + self.name,
            "purpose": "Isolated synthetic personal-state, attention and recovery qualification.",
            "op_id": self.operation("channel"),
        })
        return result["conversation"]["id"]

    def send(self, client, channel, name, text, root=""):
        return operator(client, "/posts", {"channel_id": channel, "root_id": root, "message": text,
                                          "op_id": self.operation(name)})["post"]

    def incoming(self, client, suffix, kind, root="", channel=""):
        return arrive(self.fixture, self.name + "-" + suffix, kind, root=root, channel=channel, human=client)

    def inbox(self, client):
        items, cursor, seen = {}, None, set()
        while True:
            page = operator(client, "/workspace/inbox?" + urlencode({"limit": 50, **({"before": cursor} if cursor else {})}))
            if page.get("complete") is not True:
                raise AssertionError("Inbox reports incomplete eligibility coverage")
            for item in page["items"]:
                if item["post_id"] in items:
                    raise AssertionError("Inbox duplicated a canonical message")
                items[item["post_id"]] = item
            cursor = page.get("next_before")
            if not cursor:
                if page["total"] != len(items):
                    raise AssertionError("Inbox total disagrees with complete pending-message pagination")
                return items
            if cursor in seen:
                raise AssertionError("Inbox cursor repeated")
            seen.add(cursor)

    def follow(self, client, root, enabled):
        state = operator(client, "/workspace")
        return operator(client, "/workspace/follows/" + root, {"enabled": enabled, "revision": state["revision"]}, method="PUT")

    def exercise(self):
        a, b = self.human(0), self.human(1)
        channel = self.channel()
        historical, _ = self.agent.json("/plugins/murmur/v1/posts", {
            "actor": self.seed["agents"][0], "channel_id": channel, "root_id": "",
            "message": "Historical @" + self.plan["humans"][0]["username"] + " [" + self.name + "]",
            "op_id": self.operation("historical-mention"),
        })
        workspace = operator(a, "/workspace")
        if workspace["version"] != 1 or not isinstance(workspace["revision"], int):
            raise AssertionError("Invalid personal workspace contract")
        if historical["post"]["id"] in self.inbox(a):
            raise AssertionError("Adoption manufactured a historical attention backlog")
        operator(b, "/workspace")
        self.checks.append("quiet adoption excludes older explicit mention history")

        favorite = operator(a, "/workspace/favorites/" + channel,
                            {"enabled": True, "revision": workspace["revision"]}, method="PUT")
        if channel not in favorite["favorites"]:
            raise AssertionError("Favorite did not persist")
        operator(a, "/workspace/favorites/" + channel,
                 {"enabled": False, "revision": workspace["revision"]}, method="PUT", expected=409)
        if channel in operator(b, "/workspace")["favorites"]:
            raise AssertionError("Another human inherited a private favorite")
        self.checks.append("favorite revision conflicts and per-human ownership")

        root = self.send(a, channel, "human-root", "Human request [workspace acceptance " + self.name + "]")
        self.follow(a, root["id"], True)
        first = self.incoming(a, "first", "routine", root=root["id"])
        pending = self.inbox(a)
        item = pending[first["post_id"]]
        if not {"reply", "followed", "participated"}.issubset(item["reasons"]):
            raise AssertionError("Human-root incoming event lacks its qualifying deduplicated reasons")
        original_reasons = item["reasons"]
        self.follow(a, root["id"], False)
        if self.inbox(a)[first["post_id"]]["reasons"] != original_reasons:
            raise AssertionError("Stopping erased or reclassified an already-admitted pending item")
        suppressed = self.incoming(a, "stopped", "routine", root=root["id"])
        mention = self.incoming(a, "mention", "mention", root=root["id"])
        stopped = self.inbox(a)
        if suppressed["post_id"] in stopped or stopped[mention["post_id"]]["reasons"] != ["mention"]:
            raise AssertionError("Stop did not suppress all routine reasons while allowing explicit mention")
        self.follow(a, root["id"], True)
        resumed = self.incoming(a, "resumed", "routine", root=root["id"])
        if suppressed["post_id"] in self.inbox(a) or resumed["post_id"] not in self.inbox(a):
            raise AssertionError("Resume backfilled suppressed history or lost the new arrival")
        direct = self.incoming(a, "direct", "direct")
        if "direct" not in self.inbox(a)[direct["post_id"]]["reasons"]:
            raise AssertionError("Fresh human-addressed DM root was not admitted")
        self.checks.append("captured reasons, routine stop, explicit mention, forward-only resume and fresh own-DM root")

        pending = self.inbox(a)
        operator(a, "/context/" + first["post_id"])
        if first["post_id"] not in self.inbox(a):
            raise AssertionError("Viewing handled an Inbox item")
        handled = operator(a, "/workspace/inbox/" + first["post_id"] + "/handle",
                           {"revision": pending[first["post_id"]]["revision"]})
        if first["post_id"] in self.inbox(a) or resumed["post_id"] not in self.inbox(a):
            raise AssertionError("Handle changed more than one canonical message")
        later = self.incoming(a, "after-handle", "routine", root=root["id"])
        state = operator(a, "/workspace")
        note = "Private fixture note " + self.name
        operator(a, "/workspace/saved/" + resumed["post_id"],
                 {"saved": True, "note": note, "revision": state["revision"]}, method="PUT")
        operator(a, "/workspace/inbox/" + first["post_id"] + "/undo", {"token": handled["undo_token"]})
        restored = self.inbox(a)
        if first["post_id"] not in restored or later["post_id"] not in restored:
            raise AssertionError("Undo overwrote later attention or failed to restore its exact item")
        operator(a, "/workspace/inbox/" + first["post_id"] + "/undo", {"token": handled["undo_token"]}, expected=409)
        saved = operator(a, "/workspace/saved")
        if not any(row["post_id"] == resumed["post_id"] and row["note"] == note for row in saved["items"]):
            raise AssertionError("Handle/Undo overwrote an unrelated saved note")
        preferences, _ = a.json("/api/v4/users/" + a.user_id + "/preferences")
        if not any(row["category"] == "flagged_post" and row["name"] == resumed["post_id"] and row["value"] == "true"
                   for row in (preferences or [])):
            raise AssertionError("Saved message lacks native human Saved preference authority")
        again = self.human(0)
        if channel not in operator(again, "/workspace")["favorites"] or operator(again, "/workspace/saved") != saved:
            raise AssertionError("Personal state did not survive a new authenticated HTTP session")
        if operator(b, "/workspace/saved")["items"] or self.inbox(b):
            raise AssertionError("Another observer human received private saved/Inbox state")
        self.checks.append("view versus handle, exact Undo with newer arrivals/private notes, stale token rejection and native Saved persistence")

        recovered = self.recover_send(a, channel)
        operator(b, "/operations/" + self.operation("discarded-response"), expected=(403, 404))
        before_rejection = all_posts(a, channel)
        rejected = {"channel_id": channel, "root_id": "", "message": "", "op_id": self.operation("rejected-send")}
        failure = operator(a, "/posts", rejected, expected=400)
        if failure.get("status") != "failed" or failure.get("dispatch") != "rejected":
            raise AssertionError("Rejected send lacks the exact durable no-dispatch result")
        operator(a, "/posts", {**rejected, "message": "Changed frozen rejection"}, expected=409)
        if all_posts(a, channel) != before_rejection:
            raise AssertionError("A rejected or repurposed operation created a canonical message")
        self.checks.append("other-human recovery denial and durable rejection cannot be repurposed or create a post")
        own = operator(a, "/your-messages?" + urlencode({"q": self.name, "limit": 50}))["posts"]
        if root["id"] not in {post["id"] for post in own} or recovered not in {post["id"] for post in own}:
            raise AssertionError("Own-message recall missed an operator-authored canonical message")
        if any(post["user_id"] != a.user_id for post in own):
            raise AssertionError("Own-message recall included another author's post")
        self.checks.append("canonical own-author recall includes recovered human send")
        return {"channel_id": channel, "human_root": root["id"], "direct_post": direct["post_id"],
                "saved_post": resumed["post_id"], "recovered_send": recovered}

    def recover_send(self, client, channel):
        operation = self.operation("discarded-response")
        payload = {"channel_id": channel, "root_id": "", "message": "Discarded-response send [" + self.name + "]", "op_id": operation}
        status = operator(client, "/operations/" + operation, expected=(200, 404))
        connection = None
        try:
            if not status or status.get("status") != "complete":
                if status and status.get("status") not in (None, "pending", "uncertain"):
                    raise AssertionError("Existing recovery operation has an unexpected terminal result")
                if not status or status.get("status") is None:
                    parsed = urlsplit(self.fixture.origin)
                    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                    connection = connection_type(parsed.hostname, parsed.port, timeout=30)
                    connection.request("POST", OPERATOR + "/posts", json.dumps(payload), {
                        "Authorization": "Bearer " + client.acceptance_token, "Content-Type": "application/json",
                        "X-Murmur-User-Id": client.user_id, "X-Requested-With": "XMLHttpRequest"})
                deadline = time.monotonic() + 15
                while True:
                    status = operator(client, "/operations/" + operation, expected=(200, 404))
                    if status and status.get("status") == "complete":
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("Original send remains unresolved; retain its operation ID without reposting")
                    time.sleep(0.1)
        finally:
            if connection:
                connection.close()  # Its response is deliberately never read.
        canonical = operator(client, "/posts", payload)["post"]
        if status.get("result", {}).get("post_id") != canonical["id"] or canonical["user_id"] != client.user_id or canonical["channel_id"] != channel:
            raise AssertionError("Recovery resolved another canonical post, author or destination")
        if len([post for post in all_posts(client, channel).values() if post["message"] == payload["message"]]) != 1:
            raise AssertionError("Recovered send created duplicate canonical messages")
        self.checks.append("discarded real HTTP response, exact operation recovery and one canonical send")
        return canonical["id"]

    def close(self):
        for client in self.sessions:
            client.call("/api/v4/users/logout", {}, method="POST", expected=(200, 401))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    parser.add_argument("--run", required=True, help="Stable named run; retains test humans, operation IDs and evidence")
    args = parser.parse_args()
    run = None
    try:
        fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
        run = Run(fixture, args.run)
        health, _ = run.agent.json("/plugins/murmur/v1/health")
        installed = read_private_json(fixture.state / "test-installation.json")["build"]
        if health["build"]["source_sha256"] != installed["source_sha256"]:
            raise ValueError("Installed backend identity differs from the fixture build receipt; hold acceptance")
        report = run.home / "result.json"
        if report.exists() and read_private_json(report).get("complete") is True:
            if read_private_json(report).get("backend_build") != installed:
                raise ValueError("Completed run belongs to another installed build; use a new named run without deleting the old one")
            print(json.dumps({"report": str(report), "already_complete": True}))
            return
        capabilities = operator(run.admin, "/bootstrap").get("capabilities", {})
        if not all(capabilities.get(name) is True for name in ("personal_workspace", "own_messages")):
            raise ValueError("Install the frozen personal-workspace/directory candidate before running; no test writes dispatched")
        before = protected_snapshot(fixture)
        try:
            result = run.exercise()
        finally:
            try:
                run.close()
            finally:
                after = protected_snapshot(fixture)
                if before != after:
                    raise AssertionError("Acceptance changed existing demo credentials/preferences/seeded history")
        private_json(report, {"complete": True, "origin": fixture.origin, "checks": run.checks,
                              "backend_build": installed,
                              "protected_snapshot_sha256": fingerprint(before), "result": result,
                              "limits": ["Dedicated synthetic humans/channel only", "Ordinary original-history coverage; ambiguous/purged edit-history warning is separate",
                                         "Browser draft locks/lifecycle require separate walkthrough"]})
    except (OSError, ValueError, AssertionError, KeyError) as error:
        if run is not None:
            private_json(run.home / "failure.json", {"origin": run.fixture.origin, "run": run.name,
                         "checks_passed": run.checks, "error": str(error), "complete": False})
        parser.exit(1, str(error) + "; preserve this --run for diagnosis, do not reset fixture state\n")
    print(json.dumps({"report": str(report), "checks_passed": len(run.checks)}, sort_keys=True))


if __name__ == "__main__":
    main()
