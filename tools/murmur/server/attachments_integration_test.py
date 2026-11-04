#!/usr/bin/env python3
"""Attachment acceptance on an explicitly marked disposable Mattermost server.

Uses synthetic server identities, tiny generated files and an isolated ledger.
Importing this module selects no server or credentials. --restart only disables
and enables the selected fixture's Murmur plugin after completed operations; it
does not claim to force a crash inside a native upload write.
"""

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger
from murmur.native import NativeIdentity
from server.fixture import fixture_arguments, load_fixture
from server.operator_integration_test import AGENT, OPERATOR, Server


def uid():
    return str(uuid.uuid4())


def fixtures():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b""))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>",
               b"<< /Length 0 >>\nstream\n\nendstream"]
    pdf, offsets = b"%PDF-1.4\n", [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 5\n0000000000 65535 f \n"
    pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    pdf += b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    html = b"<!doctype html><title>Attachment fixture</title><script>throw Error('must stay a download')</script>"
    return [("pixel.png", png, "image/png"), ("review.pdf", pdf, "application/pdf"),
            ("report.html", html, "text/html")]


def raw(server, path, body=None, *, token=None, headers=None, expected=200, method=None):
    values = dict(headers or {})
    if token:
        values["Authorization"] = "Bearer " + token
    request = Request(server.base + path, data=body, headers=values, method=method)
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        statuses = expected if isinstance(expected, tuple) else (expected,)
        if response.status not in statuses:
            # Do not print credentials, request headers or an authentication body.
            raise AssertionError(f"{request.get_method()} {urlsplit(path).path}: expected {statuses}, got {response.status}")
        return response.read(), response.headers


def upload_query(actor, channel, filename, body, operation=None):
    query = {"op_id": operation or uid(), "channel_id": channel, "filename": filename,
             "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    if actor is not None:
        query["actor"] = actor
    return query


def upload(server, actor, channel, filename, body, *, operation=None, operator=False, expected=200):
    query = upload_query(None if operator else actor, channel, filename, body, operation)
    prefix = OPERATOR if operator else AGENT
    headers = {"Content-Type": "application/octet-stream"}
    if operator:
        headers["X-Murmur-User-Id"] = server.user_id
    content, _ = raw(server, prefix + "/files?" + urlencode(query), body,
                     token=server.admin if operator else server.coordinator,
                     headers=headers, expected=expected)
    return json.loads(content), query


def posted(server, actor, channel, message="", files=(), root="", operation=None, *, operator=False):
    payload = {"op_id": operation or uid(), "channel_id": channel, "message": message, "root_id": root}
    if files:
        payload["file_ids"] = list(files)
    if operator:
        return server.operator("/posts", payload)["post"]
    payload["actor"] = actor
    return server.call(AGENT + "/posts", payload, token=server.coordinator)["post"]


def file_path(post, file_id, actor=None, *, operator=False, info=False):
    path = (OPERATOR if operator else AGENT) + f"/posts/{post}/files/{file_id}"
    if info:
        path += "/info"
    return path + ("?" + urlencode({"actor": actor}) if actor is not None else "")


def check_file(server, actor, post, item, body, *, operator=False):
    headers = {"X-Murmur-User-Id": server.user_id} if operator else {}
    token = server.admin if operator else server.coordinator
    metadata, _ = raw(server, file_path(post, item["id"], None if operator else actor,
                                      operator=operator, info=True), token=token, headers=headers)
    info = json.loads(metadata)["file"]
    assert (info["id"], info["name"], info["size"]) == (item["id"], item["name"], len(body))
    downloaded, response = raw(server, file_path(post, item["id"], None if operator else actor,
                                               operator=operator), token=token, headers=headers)
    assert downloaded == body, "Downloaded bytes differ from the original upload"
    assert response.get("Content-Disposition", "").startswith("attachment;")
    assert response.get("X-Content-Type-Options") == "nosniff"
    assert "private" in response.get("Cache-Control", "") and "no-store" in response.get("Cache-Control", "")
    assert response.get("Content-Type", "").split(";")[0] == "application/octet-stream"
    assert response.get("X-Content-SHA256") == hashlib.sha256(body).hexdigest()


def discard_response(server, path, body, headers, operation_path):
    """Lose only the HTTP response, after another request proves completion."""
    parsed = urlsplit(server.base)
    transport = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = transport(parsed.hostname, parsed.port, timeout=30)
    connection.request("POST", path, body, headers)
    try:
        deadline = time.monotonic() + 20
        while True:
            result = server.call(operation_path, token=server.coordinator, expected=(200, 404))
            if result.get("status") == "complete":
                return result
            if result.get("status") in {"failed", "uncertain"} or time.monotonic() >= deadline:
                raise AssertionError("Discarded-response operation did not complete; do not resend it")
            time.sleep(0.1)
    finally:
        # No getresponse/read: the original response was not consumed.
        connection.close()


class LoseOneUploadReceipt(Backend):
    """Thin client fault: discard one successful real-server upload receipt."""
    def __init__(self, url, token):
        super().__init__(url, token)
        self.upload_calls = 0
        self.post_calls = 0

    def upload(self, *args):
        self.upload_calls += 1
        result = super().upload(*args)
        if self.upload_calls == 1:
            raise BackendError("Fixture discarded the first successful upload receipt", uncertain=True)
        return result

    def mutate(self, action, *args, **kwargs):
        if action == "post":
            self.post_calls += 1
        return super().mutate(action, *args, **kwargs)


def run(fixture, restart=False):
    server = Server(fixture.coordinator, fixture.admin)
    backend = Backend(server.base + AGENT, server.coordinator)
    health = backend.request("GET", "/health")
    capability = health["capabilities"]["attachments"]
    assert capability["enabled"] and capability["max_file_size"] > 0, "The selected fixture lacks attachment support"
    with tempfile.TemporaryDirectory(prefix="murmur-attachment-http-") as directory:
        ledger = Ledger(Path(directory) / "operations.sqlite3")
        runtime = Coordinator(ledger, backend)
        try:
            def enroll(label, parent=None):
                identity = NativeIdentity("codex", uid(), parent_session_id=parent.session_id if parent else None,
                                          parent_known=True)
                runtime.register(identity, "Attachment fixture " + label, inherit="fresh")
                return identity

            parent, peer, visitor = (enroll(label) for label in ("parent", "peer", "visitor"))
            child = enroll("observer", parent)
            public = runtime.mutate(parent.actor, "conversation", kind="channel", name="Attachment acceptance",
                                    members=[peer.actor])["conversation"]["id"]
            private = runtime.mutate(parent.actor, "conversation", kind="dm", members=[peer.actor])["conversation"]["id"]
            def local_counts(actor):
                return {table: ledger.db.execute(f"SELECT count(*) FROM {table} WHERE actor=?", (actor,)).fetchone()[0]
                        for table in ("conversations", "receipts", "attempts", "outbound")}

            visitor_before = local_counts(visitor.actor)
            runtime.sync(peer.actor)
            records = []
            for index, (filename, body, media) in enumerate(fixtures()):
                uploaded, query = upload(server, parent.actor, public, filename, body)
                item = uploaded["file"]
                assert item["name"] == filename and item["size"] == len(body)
                assert item["mime_type"].split(";")[0] == media
                if index == 0:
                    runtime.sync(peer.actor)
                    assert ledger.unread_posts(peer.actor) == [] and ledger.attempts(peer.actor) == [], "Uploading alone created attention"
                replay, _ = upload(server, parent.actor, public, filename, body, operation=query["op_id"])
                assert replay["file"]["id"] == item["id"], "Upload replay created a different native file"
                status = backend.operation(parent.actor, query["op_id"])
                assert status["status"] == "complete"
                assert status["result"]["file_id"] == item["id"] and status["result"]["channel_id"] == public
                assert status["result"]["file"] == item, "Upload reconciliation must retain the returned safe metadata"
                if index == 0:
                    upload(server, parent.actor, public, "changed.png", body, operation=query["op_id"], expected=409)
                    assert backend.operation(parent.actor, query["op_id"]) == status
                post = posted(server, parent.actor, public, "" if index != 2 else "HTML download fixture",
                              [item["id"]], records[0]["post"]["id"] if index == 1 else "")
                assert post["file_ids"] == [item["id"]]
                assert [entry["id"] for entry in post["files"]] == post["file_ids"]
                canonical = server.native("/posts/" + post["id"])
                assert canonical["file_ids"] == post["file_ids"] and canonical["message"] == post["message"]
                native_info = server.native("/files/" + item["id"] + "/info")
                assert native_info["post_id"] == post["id"] and native_info["user_id"] == canonical["user_id"]
                records.append({"file": item, "post": post, "body": body})
                if index == 0:
                    summary = next(row for row in runtime.inbox(peer.actor) if row["id"] == public)
                    assert summary["latest_post_id"] == post["id"] and summary["unread_count"] == 1
                    assert filename in summary["preview"], "Attachment-only inbox preview must identify its file"
                for actor in (peer.actor, child.actor, visitor.actor):
                    check_file(server, actor, post["id"], item, body)
                check_file(server, None, post["id"], item, body, operator=True)

            root, reply, html = records
            bundle_files = [upload(server, parent.actor, public, "bundle-" + row["file"]["name"], row["body"])[0]["file"]
                            for row in (html, root)]
            bundle = posted(server, parent.actor, public, files=[item["id"] for item in bundle_files])
            assert bundle["file_ids"] == sorted(item["id"] for item in bundle_files)
            assert [item["id"] for item in bundle["files"]] == bundle["file_ids"]
            for token in (None, server.coordinator):
                raw(server, file_path(root["post"]["id"], root["file"]["id"], operator=True), token=token,
                    headers={"Mattermost-User-Id": server.user_id, "X-Murmur-User-Id": server.user_id}, expected=401)
            raw(server, file_path(root["post"]["id"], root["file"]["id"], parent.actor),
                token=server.admin, expected=401)
            assert reply["post"]["root_id"] == root["post"]["id"]
            history = backend.posts(visitor.actor, public, 100)["posts"]
            by_id = {post["id"]: post for post in history}
            assert all(by_id[row["post"]["id"]]["file_ids"] == [row["file"]["id"]] for row in records)
            thread = backend.thread_page(visitor.actor, root["post"]["id"])
            assert {post["id"] for post in thread["posts"]} == {root["post"]["id"], reply["post"]["id"]}
            assert thread["root"]["files"][0]["id"] == root["file"]["id"]
            upload(server, visitor.actor, public, "visitor.png", root["body"], expected=403)
            server.call(AGENT + "/posts", {"actor": visitor.actor, "op_id": uid(), "channel_id": public,
                        "message": "Visitors must deliberately join"}, token=server.coordinator, expected=403)
            runtime.sync(visitor.actor)
            assert local_counts(visitor.actor) == visitor_before, "File preview enrolled or notified a public visitor"
            server.native("/channels/" + public + "/members/" + ledger.actor(visitor.actor)["user_id"], expected=404)
            runtime.sync(peer.actor)
            before = ledger.unread_posts(peer.actor, public)
            check_file(server, peer.actor, root["post"]["id"], root["file"], root["body"])
            assert ledger.unread_posts(peer.actor, public) == before, "A download consumed unread messages"
            selection = runtime.prepare_read(peer.actor, public, limit=1)
            assert len(selection["posts"]) == 1 and selection["posts"][0]["files"]
            arriving = posted(server, parent.actor, public, "Concurrent arrival after prepared attachment read")
            runtime.ack_read(peer.actor, selection)
            runtime.sync(peer.actor)
            remaining = {post["id"] for post in ledger.unread_posts(peer.actor, public)}
            assert arriving["id"] in remaining and not set(selection["ack_ids"]) & remaining
            assert len(remaining) == len(before)
            runtime.set_saved(peer.actor, root["post"]["id"], True)
            assert root["post"]["id"] in {row["post_id"] for row in backend.saved(peer.actor)}

            # Actual post binding, not arbitrary file + readable channel, grants bytes.
            for info in (False, True):
                raw(server, file_path(root["post"]["id"], reply["file"]["id"], visitor.actor, info=info),
                    token=server.coordinator, expected=404)
            secret, _ = upload(server, parent.actor, private, "private.pdf", fixtures()[1][1])
            secret_post = posted(server, parent.actor, private, files=[secret["file"]["id"]])
            check_file(server, child.actor, secret_post["id"], secret["file"], fixtures()[1][1])
            for info in (False, True):
                raw(server, file_path(secret_post["id"], secret["file"]["id"], visitor.actor, info=info),
                    token=server.coordinator, expected=403)
            upload(server, child.actor, private, "forbidden.png", root["body"], expected=403)
            child_user = ledger.actor(child.actor)["user_id"]
            server.native("/channels/" + private + "/members/" + child_user, expected=404)
            staged, _ = upload(server, parent.actor, public, "staged.png", root["body"])
            before_wrong_channel = set(server.native("/channels/" + private + "/posts")["posts"])
            server.call(AGENT + "/posts", {"actor": parent.actor, "op_id": uid(), "channel_id": private,
                        "message": "", "file_ids": [staged["file"]["id"]]}, token=server.coordinator, expected=(400, 403))
            assert set(server.native("/channels/" + private + "/posts")["posts"]) == before_wrong_channel
            raw(server, file_path(root["post"]["id"], staged["file"]["id"], parent.actor),
                token=server.coordinator, expected=404)
            foreign, _ = upload(server, peer.actor, public, "owned-by-peer.png", root["body"])
            forbidden = {"actor": parent.actor, "op_id": uid(), "channel_id": public,
                         "message": "Must not steal another upload", "file_ids": [foreign["file"]["id"]]}
            before_denied = set(server.native("/channels/" + public + "/posts")["posts"])
            server.call(AGENT + "/posts", forbidden, token=server.coordinator, expected=(400, 403))
            assert set(server.native("/channels/" + public + "/posts")["posts"]) == before_denied
            raw(server, file_path(root["post"]["id"], foreign["file"]["id"], parent.actor),
                token=server.coordinator, expected=404)
            before_attached_reuse = set(server.native("/channels/" + public + "/posts")["posts"])
            server.call(AGENT + "/posts", {"actor": parent.actor, "op_id": uid(), "channel_id": public,
                        "message": "", "file_ids": [root["file"]["id"]]}, token=server.coordinator, expected=409)
            assert set(server.native("/channels/" + public + "/posts")["posts"]) == before_attached_reuse

            # Observing/posting as the human must not require joining the agent DM.
            human = server.native("/users/me")
            assert human["id"] == server.user_id and human["delete_at"] == 0
            assert "system_admin" in human["roles"].split() and not human.get("is_bot", False)
            server.native("/channels/" + private + "/members/" + server.user_id, expected=404)
            operator_file, _ = upload(server, None, private, "operator.html", html["body"], operator=True)
            human_post = posted(server, None, private, files=[operator_file["file"]["id"]], operator=True)
            native_human_post = server.native("/posts/" + human_post["id"])
            assert native_human_post["user_id"] == server.user_id and native_human_post["channel_id"] == private
            assert native_human_post["file_ids"] == [operator_file["file"]["id"]]
            native_human_file = server.native("/files/" + operator_file["file"]["id"] + "/info")
            assert native_human_file["post_id"] == human_post["id"] and native_human_file["user_id"] == server.user_id
            check_file(server, parent.actor, human_post["id"], operator_file["file"], html["body"])
            server.native("/channels/" + private + "/members/" + server.user_id, expected=404)
            raw(server, file_path(human_post["id"], operator_file["file"]["id"], operator=True), token=server.admin,
                headers={"X-Murmur-User-Id": "wrong-bound-human"}, expected=409)

            # Standard native-client upload needs explicit team/channel membership.
            team_id = server.native("/channels/" + public)["team_id"]
            membership = server.native("/teams/" + team_id + "/members/" + server.user_id, expected=(200, 404))
            if membership.get("status_code") == 404:
                server.native("/teams/" + team_id + "/members", {"team_id": team_id, "user_id": server.user_id}, expected=201)
            membership = server.native("/teams/" + team_id + "/members/" + server.user_id)
            assert membership["team_id"] == team_id and membership["user_id"] == server.user_id
            server.native("/channels/" + public + "/members", {"user_id": server.user_id}, expected=201)
            native_body, _ = raw(server, "/api/v4/files?" + urlencode({"channel_id": public, "filename": "native.png"}),
                                 root["body"], token=server.admin, headers={"Content-Type": "application/octet-stream"},
                                 expected=201)
            native_file = json.loads(native_body)["file_infos"][0]
            native_post = server.native("/posts", {"channel_id": public, "message": "", "file_ids": [native_file["id"]]}, expected=201)
            view = backend.post(visitor.actor, native_post["id"])
            assert view["file_ids"] == [native_file["id"]] and view["files"][0]["name"] == "native.png"
            check_file(server, visitor.actor, native_post["id"], view["files"][0], root["body"])
            server.native("/posts/" + native_post["id"] + "/patch",
                          {"message": "Attachment removed by fixture", "file_ids": []}, method="PUT")
            assert server.native("/posts/" + native_post["id"]).get("file_ids", []) == []
            for info in (False, True):
                raw(server, file_path(native_post["id"], native_file["id"], visitor.actor, info=info),
                    token=server.coordinator, expected=404)

            # Losing an HTTP response is distinct from aborting an unexecuted request.
            lost_query = upload_query(parent.actor, public, "lost-response.html", html["body"])
            lost_path = AGENT + "/files?" + urlencode(lost_query)
            upload_operation = AGENT + "/operations/" + lost_query["op_id"] + "?" + urlencode({"actor": parent.actor})
            recovered_upload = discard_response(server, lost_path, html["body"], {
                "Authorization": "Bearer " + server.coordinator, "Content-Type": "application/octet-stream"}, upload_operation)
            lost_file = recovered_upload["result"]["file_id"]
            lost_post = {"actor": parent.actor, "op_id": uid(), "channel_id": public, "message": "", "file_ids": [lost_file]}
            post_operation = AGENT + "/operations/" + lost_post["op_id"] + "?" + urlencode({"actor": parent.actor})
            recovered_post = discard_response(server, AGENT + "/posts", json.dumps(lost_post).encode(), {
                "Authorization": "Bearer " + server.coordinator, "Content-Type": "application/json"}, post_operation)
            replay = server.call(AGENT + "/posts", lost_post, token=server.coordinator)["post"]
            assert replay["id"] == recovered_post["result"]["post_id"] and replay["file_ids"] == [lost_file]
            all_posts = server.native("/channels/" + public + "/posts")["posts"].values()
            assert sum(lost_file in post.get("file_ids", []) for post in all_posts) == 1
            check_file(server, peer.actor, replay["id"], replay["files"][0], html["body"])
            if restart:
                server.native("/plugins/murmur/disable", {})
                server.native("/plugins/murmur/enable", {})
                assert backend.operation(parent.actor, lost_query["op_id"]) == recovered_upload
                assert backend.operation(parent.actor, lost_post["op_id"]) == recovered_post
                check_file(server, child.actor, replay["id"], replay["files"][0], html["body"])

            # Exercise the actual client parent/upload executor against this server.
            source = Path(directory) / "runtime.png"
            source.write_bytes(root["body"])
            normal = runtime.send(parent.actor, public, "", files=[source])["post"]
            destination = Path(directory) / "downloaded.png"
            downloaded = runtime.download(peer.actor, normal["id"], normal["file_ids"][0], destination)
            assert destination.read_bytes() == root["body"] and downloaded["size"] == len(root["body"])
            assert downloaded["sha256"] == hashlib.sha256(root["body"]).hexdigest()
            fault = LoseOneUploadReceipt(server.base + AGENT, server.coordinator)
            before_parents = {operation["op_id"] for operation in ledger.outbounds(parent.actor)}
            try:
                Coordinator(ledger, fault).send(parent.actor, public, "", files=[source])
                raise AssertionError("The injected lost upload receipt must hold the original send")
            except BackendError as error:
                assert error.uncertain
            new_parents = [operation for operation in ledger.outbounds(parent.actor)
                           if operation["op_id"] not in before_parents and operation["payload"].get("action") == "attachment_post"]
            assert len(new_parents) == 1 and fault.upload_calls == 1 and fault.post_calls == 0
            parent_id = new_parents[0]["op_id"]
            ledger.close()
            ledger = Ledger(Path(directory) / "operations.sqlite3")
            runtime = Coordinator(ledger, fault)
            for _ in range(4):
                runtime.reconcile_outbound(parent.actor)
                if ledger.outbound(parent_id)["state"] == "complete":
                    break
            resumed = ledger.outbound(parent_id)
            assert resumed["state"] == "complete" and fault.upload_calls == 1 and fault.post_calls == 1
            runtime.reconcile_outbound(parent.actor)
            assert fault.upload_calls == 1 and fault.post_calls == 1, "Completed parent was dispatched again"
            resumed_post = backend.post(parent.actor, resumed["result"]["post_id"])
            second_destination = Path(directory) / "recovered.png"
            runtime.download(peer.actor, resumed_post["id"], resumed_post["file_ids"][0], second_destination)
            assert second_destination.read_bytes() == root["body"]

            runtime.sync(peer.actor)
            for channel in (public, private):
                ledger.set_mute(peer.actor, channel, True)
            muted_post = posted(server, parent.actor, public, files=[staged["file"]["id"]])
            runtime.sync(peer.actor)
            muted = next(post for post in ledger.unread_posts(peer.actor, public) if post["id"] == muted_post["id"])
            assert muted["read_at"] is None and not muted["notification_eligible"]
            assert ledger.create_attempt(peer.actor, "attachment-fixture") is None
            check_file(server, peer.actor, muted_post["id"], staged["file"], root["body"])
            assert muted_post["id"] in {post["id"] for post in ledger.unread_posts(peer.actor, public)}
            ledger.set_mute(peer.actor, public, False)
            assert ledger.create_attempt(peer.actor, "attachment-fixture") is None, "Unmute revived suppressed attachment mail"

            # Deleted canonical posts revoke both metadata and bytes, including observers.
            server.native("/posts/" + secret_post["id"], method="DELETE")
            for info in (False, True):
                raw(server, file_path(secret_post["id"], secret["file"]["id"], child.actor, info=info),
                    token=server.coordinator, expected=404)
            return {"formats": [item[0] for item in fixtures()], "attachment_only_root": root["post"]["id"],
                    "attachment_only_reply": reply["post"]["id"], "native_client_post": native_post["id"],
                    "human_attachment_post": human_post["id"], "discarded_upload_recovered": lost_file,
                    "discarded_post_recovered": replay["id"], "completed_operation_restart": restart,
                    "runtime_parent_post": normal["id"], "runtime_resumed_parent_post": resumed_post["id"],
                    "runtime_lost_receipt_upload_calls": fault.upload_calls,
                    "muted_attachment_post": muted_post["id"],
                    "native_sessions_started": 0}
        finally:
            ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Mutate only the explicitly selected marked fixture")
    parser.add_argument("--restart", action="store_true", help="Restart only the selected fixture plugin after completed operations")
    fixture_arguments(parser)
    args = parser.parse_args()
    if not args.run:
        parser.error("--run and an explicitly marked disposable installation are required")
    fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
    print(json.dumps(run(fixture, args.restart), indent=2))


if __name__ == "__main__":
    main()
