"""Attachment stages: bytes, exact post intent, uncertainty and quiet downloads."""

from contextlib import redirect_stdout
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli
from murmur import control
from murmur.attachments import describe_source, source_bytes
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger
import test_coordinator as fixtures


class AttachmentBackend(fixtures.FakeBackend):
    def __init__(self):
        super().__init__()
        self.upload_calls = []
        self.lose_upload = False
        self.commit_upload = True
        self.before_upload = None

    def attachment_limits(self):
        return {"enabled": True, "max_file_size": 1024, "max_files_per_post": 10}

    def upload(self, actor, op_id, channel, source, data):
        if self.before_upload:
            self.before_upload(op_id)
        self.upload_calls.append((op_id, source, data))
        info = {"id": "file-" + op_id, "name": source["filename"], "size": len(data),
                "mime_type": "application/octet-stream"}
        result = {"file": info, "file_id": info["id"], "channel_id": channel}
        if self.commit_upload:
            self.operations[op_id] = {"status": "complete", "result": result}
        if self.lose_upload:
            raise BackendError("Upload response lost", uncertain=True)
        return result

    def mutate(self, action, actor, op_id, **payload):
        self.mutation_calls.append((action, actor, op_id, payload))
        post = {"id": "post-" + op_id, "channel_id": payload["channel_id"], "user_id": "self",
                "message": payload["message"], "root_id": payload.get("root_id", ""),
                "file_ids": sorted(payload.get("file_ids", []))}
        result = {"post": post}
        if self.commit_before_failure:
            self.operations[op_id] = {"status": "complete", "result": {
                "post_id": post["id"], "channel_id": post["channel_id"]}}
        if self.lose_response:
            raise BackendError("Post response lost", uncertain=True)
        return result


class AttachmentCoordinatorTests(unittest.TestCase):
    def setUp(self):
        fixtures.CoordinatorTests.setUp(self)
        self.backend = AttachmentBackend()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.files = [Path(self.home.name) / name for name in ("image.png", "notes.pdf")]
        for file, data in zip(self.files, (b"image-contents", b"pdf-contents")):
            file.write_bytes(data)

    tearDown = fixtures.CoordinatorTests.tearDown

    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch("murmur.native.current_identity", return_value=self.identity), redirect_stdout(output):
            cli.main(list(arguments))
        return json.loads(output.getvalue())

    def parent(self):
        return next(operation for operation in self.ledger.outbounds(self.actor)
                    if operation["payload"]["action"] == "attachment_post")

    def test_attachment_only_send_and_reply_freeze_canonical_ids_before_post(self):
        def verify_claim(op_id):
            operation = self.ledger.outbound(op_id)
            self.assertEqual(operation["state"], "uncertain")
            self.assertEqual(len(self.ledger.outbounds(self.actor)), 3)
        self.backend.before_upload = verify_claim
        result = self.run_cli("send", "build", "--file", str(self.files[0]), "--file", str(self.files[1]))
        self.assertEqual(result["post"]["message"], "")
        self.assertEqual(result["post"]["file_ids"], sorted("file-" + row[0] for row in self.backend.upload_calls))
        self.assertEqual(len(self.backend.mutation_calls), 1)
        self.assertEqual(self.parent()["state"], "complete")
        self.assertNotIn("data", self.parent()["payload"])
        self.backend.add("root")
        self.backend.before_upload = None
        reply = self.run_cli("reply", "root", "--file", str(self.files[0]), "Please review")
        self.assertEqual((reply["post"]["root_id"], reply["post"]["message"]), ("root", "Please review"))

    def test_text_before_or_after_flags_and_text_file_remain_literal(self):
        self.backend.add("root")
        for arguments in (("send", "build", "Caption", "--file", str(self.files[0])),
                          ("send", "build", "--file", str(self.files[0]), "Caption"),
                          ("send", "build", "--thread", "root", "Caption", "--file", str(self.files[0]))):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments)["post"]["message"], "Caption")
        with self.assertRaisesRegex(ValueError, "not both"):
            self.run_cli("send", "build", "Caption", "--text-file", str(self.files[0]))

    def test_lost_upload_response_restart_resumes_original_parent_without_reupload(self):
        self.backend.lose_upload = True
        self.ledger.db.execute("UPDATE actors SET causal_json=? WHERE actor=?", ('["original"]', self.actor))
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "Original caption", files=self.files)
        parent = self.parent()
        self.assertEqual(len(self.backend.upload_calls), 1)
        self.assertEqual(self.backend.mutation_calls, [])
        self.files[0].unlink()  # The confirmed first upload must not reread its old local source.
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.backend.lose_upload = False
        self.ledger.db.execute("UPDATE actors SET causal_json=? WHERE actor=?", ('["later"]', self.actor))
        self.coordinator.reconcile_outbound(self.actor)
        self.coordinator.reconcile_outbound(self.actor)
        self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(self.ledger.outbound(parent["op_id"])["state"], "complete")
        self.assertEqual(len(self.backend.upload_calls), 2)
        self.assertEqual(len(self.backend.mutation_calls), 1)
        payload = self.backend.mutation_calls[0][3]
        self.assertEqual(payload["causal"], {"chain_ids": ["original"]})
        self.assertEqual(payload["file_ids"], sorted("file-" + child for child in parent["payload"]["uploads"]))

    def test_reverse_uploaded_ids_are_canonical_before_claim_and_native_normalization(self):
        uploaded_ids = iter(("z" * 26, "a" * 26))
        expected = ["a" * 26, "z" * 26]
        original_upload, original_mutate = self.backend.upload, self.backend.mutate
        def upload(*arguments):
            result = original_upload(*arguments)
            result["file_id"] = result["file"]["id"] = next(uploaded_ids)
            return result
        def native_post(action, actor, op_id, **payload):
            self.assertEqual(payload["file_ids"], expected)
            claim = self.ledger.outbound(op_id)
            self.assertEqual((claim["state"], claim["result"]["file_ids"]), ("uncertain", expected))
            # The fake's native save sorts a separate list, as the HTTP boundary
            # prevents native normalization from rewriting client-side intent.
            return original_mutate(action, actor, op_id, **payload)
        self.backend.upload, self.backend.mutate = upload, native_post
        result = self.coordinator.send(self.actor, "build", "", files=self.files)
        self.assertEqual(result["post"]["file_ids"], expected)
        self.assertEqual(self.parent()["state"], "complete")
        self.assertEqual((len(self.backend.upload_calls), len(self.backend.mutation_calls)), (2, 1))

    def test_duplicate_uploaded_ids_are_rejected_without_posting_or_deduplication(self):
        original = self.backend.upload
        def duplicate(*arguments):
            result = original(*arguments)
            result["file_id"] = result["file"]["id"] = "a" * 26
            return result
        self.backend.upload = duplicate
        with self.assertRaisesRegex(BackendError, "distinct"):
            self.coordinator.send(self.actor, "build", "", files=self.files)
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertNotEqual(self.parent()["state"], "complete")

    def test_uncertain_upload_with_missing_server_operation_is_held_not_replayed(self):
        self.backend.lose_upload, self.backend.commit_upload = True, False
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "", files=self.files)
        for _ in range(3):
            self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(len(self.backend.upload_calls), 1)
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.parent()["state"], "pending")
        self.assertIn("not confirmed", self.ledger.outbound(self.parent()["payload"]["uploads"][0])["error"])

    def test_lost_post_response_is_reconciled_once_and_no_files_are_reuploaded(self):
        self.backend.lose_response = True
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "One", files=self.files[:1])
        parent = self.parent()
        self.assertEqual(parent["state"], "uncertain")
        def deleted(*_arguments):
            raise BackendError("Post was later deleted", status=404)
        self.backend.post = deleted
        self.coordinator.reconcile_outbound(self.actor)
        self.coordinator.reconcile_outbound(self.actor)
        complete = self.ledger.outbound(parent["op_id"])
        self.assertEqual(complete["state"], "complete")
        self.assertEqual(complete["result"], {"post_id": "post-" + parent["op_id"], "channel_id": "build"})
        self.assertEqual((len(self.backend.upload_calls), len(self.backend.mutation_calls)), (1, 1))

    def test_competing_coordinator_cannot_dispatch_a_claimed_upload_again(self):
        parent = self.ledger.prepare_attachment_outbound(self.actor,
            {"channel_id": "build", "message": "", "root_id": ""}, [describe_source(self.files[0], 1024)])
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked(_op_id):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test failed to release the original upload")
        def first_coordinator():
            ledger = Ledger(self.path)
            try:
                Coordinator(ledger, self.backend)._advance_attachment(parent)
            except BaseException as error:
                errors.append(error)
            finally:
                ledger.close()
        self.backend.before_upload = blocked
        worker = threading.Thread(target=first_coordinator)
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            self.coordinator._advance_attachment(parent)
            self.assertEqual(self.backend.upload_calls, [])
        finally:
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.coordinator._advance_attachment(parent)
        self.assertEqual((len(self.backend.upload_calls), len(self.backend.mutation_calls)), (1, 1))
        self.assertEqual(self.ledger.outbound(parent["op_id"])["state"], "complete")

    def test_text_only_payload_and_file_limit_failure_create_no_attachment_stages(self):
        self.coordinator.send(self.actor, "build", "Plain text")
        operations = self.ledger.outbounds(self.actor)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["payload"], {"action": "post", "channel_id": "build",
            "message": "Plain text", "root_id": ""})
        self.backend.attachment_limits = lambda: {"max_file_size": 1024, "max_files_per_post": 1}
        with self.assertRaises(ValueError):
            self.coordinator.send(self.actor, "build", "", files=self.files)
        self.assertEqual(len(self.ledger.outbounds(self.actor)), 1)
        self.assertEqual(self.backend.upload_calls, [])

    def test_restart_before_upload_dispatch_can_finish_but_claim_before_crash_is_held(self):
        source = describe_source(self.files[0], 1024)
        parent = self.ledger.prepare_attachment_outbound(self.actor,
            {"channel_id": "build", "message": "", "root_id": ""}, [source])
        self.coordinator.reconcile_outbound(self.actor, operations=[parent])
        self.coordinator.reconcile_outbound(self.actor, operations=[parent])
        self.assertEqual(self.ledger.outbound(parent["op_id"])["state"], "complete")
        second = self.ledger.prepare_attachment_outbound(self.actor,
            {"channel_id": "build", "message": "", "root_id": ""}, [source])
        self.ledger.claim_outbound(second["payload"]["uploads"][0])
        self.coordinator.reconcile_outbound(self.actor, operations=[second])
        self.assertEqual(len(self.backend.upload_calls), 1)
        self.assertEqual(self.ledger.outbound(second["payload"]["uploads"][0])["state"], "uncertain")

    def test_source_mutation_and_empty_file_fail_locally_without_upload(self):
        for content in (b"changed-data", None):
            source = describe_source(self.files[0], 1024)
            parent = self.ledger.prepare_attachment_outbound(self.actor,
                {"channel_id": "build", "message": "", "root_id": ""}, [source])
            if content is None:
                self.files[0].unlink()
            else:
                self.files[0].write_bytes(content)
            with self.assertRaises((ValueError, OSError)):
                self.coordinator._advance_attachment(parent)
            self.assertEqual(self.ledger.outbound(parent["op_id"])["state"], "failed")
            self.assertEqual(self.backend.upload_calls, [])
            self.files[0].write_bytes(b"image-contents")
        self.files[0].write_bytes(b"")
        with self.assertRaises(ValueError):
            self.coordinator.send(self.actor, "build", "", files=self.files[:1])
        self.assertEqual(self.backend.upload_calls, [])

    def test_wrong_upload_binding_and_partial_post_stay_uncertain(self):
        original = self.backend.upload
        def wrong_channel(*arguments):
            return {**original(*arguments), "channel_id": "private"}
        self.backend.upload = wrong_channel
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "", files=self.files[:1])
        self.assertEqual(self.backend.mutation_calls, [])
        self.assertEqual(self.ledger.outbound(self.parent()["payload"]["uploads"][0])["state"], "uncertain")
        first_id = self.parent()["op_id"]
        self.backend.upload = original
        original_post = self.backend.mutate
        def drop_files(*arguments, **payload):
            value = original_post(*arguments, **payload)
            value["post"]["file_ids"] = []
            self.backend.operations[arguments[2]] = {"status": "uncertain", "error": "canonical attachment mismatch"}
            return value
        self.backend.mutate = drop_files
        with self.assertRaises(BackendError):
            self.coordinator.send(self.actor, "build", "", files=self.files[:1])
        parents = [op for op in self.ledger.outbounds(self.actor) if op["payload"]["action"] == "attachment_post"]
        second = next(op for op in parents if op["op_id"] != first_id)
        self.assertEqual(second["state"], "uncertain")
        self.coordinator.reconcile_outbound(self.actor, operations=[second])
        self.assertEqual(self.ledger.outbound(second["op_id"])["state"], "uncertain")
        self.assertEqual(len(self.backend.mutation_calls), 1)

    def test_upload_receipt_requires_matching_typed_metadata(self):
        self.files[0].write_bytes(b"x")
        source = describe_source(self.files[0], 1024)
        parent = self.ledger.prepare_attachment_outbound(self.actor,
            {"channel_id": "build", "message": "", "root_id": ""}, [source])
        child = self.ledger.outbound(parent["payload"]["uploads"][0])
        good = {"file_id": "file", "channel_id": "build", "file": {
            "id": "file", "name": source["filename"], "size": 1, "mime_type": "image/png"}}
        for bad in ({"file": None}, {"file": {**good["file"], "size": True}},
                    {"file": {**good["file"], "name": "substitute.png"}},
                    {"file": {**good["file"], "id": "other"}},
                    {"file": {**good["file"], "mime_type": None}}):
            with self.subTest(bad=bad), self.assertRaises(BackendError):
                self.coordinator._upload_result(child, {**good, **bad})

    def test_late_missing_operation_cannot_revive_failed_or_overwrite_complete_stage(self):
        for terminal in ("failed", "complete"):
            with self.subTest(terminal=terminal):
                parent = self.ledger.prepare_attachment_outbound(self.actor,
                    {"channel_id": "build", "message": "", "root_id": ""},
                    [describe_source(self.files[0], 1024)])
                child_id = parent["payload"]["uploads"][0]
                self.ledger.claim_outbound(child_id)
                queried, release = threading.Event(), threading.Event()
                errors = []
                def late_missing(*_args):
                    queried.set()
                    if not release.wait(3):
                        raise RuntimeError("Test did not release the late receipt")
                    raise BackendError("No server operation", status=404)
                self.backend.operation = late_missing
                def reader():
                    ledger = Ledger(self.path)
                    try:
                        Coordinator(ledger, self.backend)._reconcile_one(ledger.outbound(child_id))
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        ledger.close()
                worker = threading.Thread(target=reader)
                worker.start()
                self.assertTrue(queried.wait(3))
                try:
                    self.ledger.settle_outbound(child_id, terminal, result={"proof": terminal}, error="terminal proof")
                    if terminal == "failed":
                        self.ledger.fail_attachment_outbound(parent["op_id"], "Local failure before HTTP")
                finally:
                    release.set(); worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                settled = self.ledger.outbound(child_id)
                self.assertEqual((settled["state"], settled["result"], settled["error"]),
                    (terminal, {"proof": terminal}, "terminal proof"))

    def test_stop_during_sync_blocks_new_upload_and_post_but_allows_receipt_lookup(self):
        parent = self.ledger.prepare_attachment_outbound(self.actor,
            {"channel_id": "build", "message": "", "root_id": ""},
            [describe_source(self.files[0], 1024)])
        stopped = threading.Event()
        changes = self.backend.changes
        def stop_during_sync(*args, **kwargs):
            stopped.set()
            return changes(*args, **kwargs)
        settings = {"url": "http://fixture.invalid", "token": "fixture", "delivery_enabled": False}
        def tick():
            with patch.object(control.config, "load", return_value=settings), \
                    patch.object(control.config, "ledger_path", return_value=self.path), \
                    patch.object(control, "Backend", return_value=self.backend):
                return control.network_tick(Path(self.home.name), self.actor, stopped)
        self.backend.changes = stop_during_sync
        tick()
        self.assertEqual(self.backend.upload_calls, [])
        self.assertEqual(self.backend.mutation_calls, [])
        child_id = parent["payload"]["uploads"][0]
        self.assertEqual(self.ledger.outbound(child_id)["state"], "pending")
        self.backend.changes = changes
        self.backend.lose_upload = True
        with self.assertRaises(BackendError):
            self.coordinator._advance_attachment(parent)
        tick()  # stopped: GET receipt can settle the possibly written upload.
        self.assertEqual(self.ledger.outbound(child_id)["state"], "complete")
        self.assertEqual(self.ledger.outbound(parent["op_id"])["state"], "pending")
        self.assertEqual(self.backend.mutation_calls, [])
        stopped.clear()
        tick()
        self.assertEqual(len(self.backend.mutation_calls), 1)
        self.assertEqual(self.ledger.outbound(parent["op_id"])["state"], "complete")

    def test_atomic_settlement_prefers_terminal_proof_under_competing_connections(self):
        for terminal in ("complete", "failed"):
            with self.subTest(terminal=terminal):
                op = self.ledger.prepare_outbound(self.actor, {"action": "post", "channel_id": "build"})
                self.ledger.claim_outbound(op["op_id"])
                start = threading.Barrier(2)
                errors = []
                def settle(state):
                    ledger = Ledger(self.path)
                    try:
                        start.wait(3)
                        ledger.settle_outbound(op["op_id"], state, result={"proof": state})
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        ledger.close()
                workers = [threading.Thread(target=settle, args=(state,)) for state in (terminal, "uncertain")]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(4)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                final = self.ledger.outbound(op["op_id"])
                self.assertEqual((final["state"], final["result"]), (terminal, {"proof": terminal}))

    def test_stop_after_source_read_leaves_stage_unclaimed_for_next_coordinator(self):
        parent = self.ledger.prepare_attachment_outbound(self.actor,
            {"channel_id": "build", "message": "", "root_id": ""},
            [describe_source(self.files[0], 1024)])
        stopped = threading.Event()
        def finish_source(source):
            data = source_bytes(source)
            stopped.set()
            return data
        with patch("murmur.coordinator.source_bytes", side_effect=finish_source):
            self.coordinator._advance_attachment(parent, can_dispatch=lambda: not stopped.is_set())
        self.assertEqual(self.backend.upload_calls, [])
        self.assertEqual(self.ledger.outbound(parent["payload"]["uploads"][0])["state"], "pending")

    def test_file_only_preview_and_download_keep_post_attention_unchanged(self):
        post = self.backend.add("incoming")
        post.update(message="", file_ids=["image"], files=[{"id": "image", "name": "image.png", "size": 9}])
        self.coordinator.sync(self.actor)
        before = [tuple(r) for r in self.ledger.db.execute("SELECT * FROM receipts")]
        self.assertEqual(self.coordinator.inbox(self.actor)[0]["preview"], "[1 attachment: image.png]")
        self.backend.download = lambda *_args: {"file_id": "image", "path": "explicit"}
        self.assertEqual(self.coordinator.download(self.actor, "incoming", "image", "explicit")["file_id"], "image")
        self.assertEqual([tuple(r) for r in self.ledger.db.execute("SELECT * FROM receipts")], before)
        selected = self.coordinator.prepare_read(self.actor, post_id="incoming")
        self.assertEqual(selected["posts"][0]["files"][0]["name"], "image.png")
        self.coordinator.ack_read(self.actor, selected)
        self.assertEqual(self.coordinator.read(self.actor, "build"), [])


class AttachmentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = b"<!doctype html><script>not executed</script>"
        self.requests = []
        self.uploads = []
        self.uploaded = {}
        self.mode = "normal"
        outer = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization")))
                if "/operations/" in self.path:
                    op_id = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
                    body = json.dumps({"status": "complete", "result": outer.uploaded[op_id]}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                if self.path.startswith("/redirect-target"):
                    self.send_response(200); self.end_headers(); return
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers(); return
                if "/info?" in self.path:
                    data = json.dumps({"file": {"id": "file", "name": "../../evil.html", "size": len(outer.data),
                        "mime_type": "text/html"}}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(data)))
                    self.end_headers(); self.wfile.write(data); return
                self.send_response(200)
                payload = outer.data if outer.mode != "truncated" else outer.data[:-3]
                self.send_header("Content-Length", str(len(outer.data)))
                self.send_header("X-Content-SHA256", hashlib.sha256(outer.data if outer.mode != "corrupt" else b"wrong").hexdigest())
                self.end_headers(); self.wfile.write(payload)

            def do_POST(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                data = self.rfile.read(int(self.headers["Content-Length"]))
                outer.uploads.append((query, data, self.headers.get("Authorization")))
                info = {"id": "file-" + query["op_id"][0], "name": query["filename"][0],
                    "size": len(data), "mime_type": "application/octet-stream"}
                result = {"file": info, "file_id": info["id"], "channel_id": query["channel_id"][0]}
                outer.uploaded[query["op_id"][0]] = result
                if outer.mode == "uncertain409":
                    body = json.dumps({"error": "native upload is not verified: file lookup failed; bytes will not be resent"}).encode()
                    self.send_response(409); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                if outer.mode == "redirect":
                    self.send_response(307); self.send_header("Location", "/redirect-target")
                    self.end_headers(); return
                body = json.dumps(result).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.backend = Backend(f"http://127.0.0.1:{self.server.server_port}", "private-token")

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.directory.cleanup()

    def test_explicit_download_verifies_bytes_and_never_uses_server_filename_or_overwrites(self):
        output = Path(self.directory.name) / "chosen.html"
        result = self.backend.download("actor", "post", "file", output)
        self.assertEqual(output.read_bytes(), self.data)
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertFalse((Path(self.directory.name).parent / "evil.html").exists())
        with self.assertRaises(ValueError):
            self.backend.download("actor", "post", "file", output)
        self.assertEqual(output.read_bytes(), self.data)

    def test_bad_or_truncated_data_leaves_no_partial_output(self):
        for mode in ("corrupt", "truncated"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises((BackendError, OSError)):
                self.backend.download("actor", "post", "file", Path(self.directory.name) / mode)
            self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_redirect_is_refused_without_forwarding_authorization(self):
        self.mode = "redirect"
        with self.assertRaises(BackendError):
            self.backend.download("actor", "post", "file", Path(self.directory.name) / "no-file")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(any(path.startswith("/redirect-target") for path, _ in self.requests))

    def test_raw_upload_has_frozen_descriptor_and_does_not_follow_redirects(self):
        path = Path(self.directory.name) / "report λ.html"
        path.write_bytes(self.data)
        source = describe_source(path, 1024)
        result = self.backend.upload("actor", "upload-op", "channel", source, source_bytes(source))
        self.assertEqual(result["file"]["name"], path.name)
        query, data, authorization = self.uploads[0]
        self.assertEqual(data, self.data)
        self.assertEqual(query["sha256"], [hashlib.sha256(self.data).hexdigest()])
        self.assertEqual(query["op_id"], ["upload-op"])
        self.assertEqual(authorization, "Bearer private-token")
        self.mode = "redirect"
        with self.assertRaises(BackendError) as caught:
            self.backend.upload("actor", "another-op", "channel", source, source_bytes(source))
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(self.requests, [])

    def test_actual_uncertain409_wire_recovers_original_parent_without_reupload(self):
        ledger = Ledger(Path(self.directory.name) / "ledger.sqlite3")
        try:
            identity = fixtures.NativeIdentity("codex", fixtures.SESSION, parent_known=True)
            backend = AttachmentBackend()
            backend.upload, backend.operation = self.backend.upload, self.backend.operation
            runtime = Coordinator(ledger, backend)
            runtime.register(identity, "Attachment test")
            source = Path(self.directory.name) / "notes.html"
            source.write_bytes(self.data)
            self.mode = "uncertain409"
            with self.assertRaises(BackendError) as caught:
                runtime.send(identity.key, "build", "", files=[source])
            self.assertTrue(caught.exception.uncertain)
            parent = next(op for op in ledger.outbounds(identity.key) if op["payload"]["action"] == "attachment_post")
            child_id = parent["payload"]["uploads"][0]
            self.assertEqual(ledger.outbound(child_id)["state"], "uncertain")
            self.assertEqual(ledger.outbound(parent["op_id"])["state"], "pending")
            self.mode = "normal"
            runtime.reconcile_outbound(identity.key)
            runtime.reconcile_outbound(identity.key)
            self.assertEqual(ledger.outbound(parent["op_id"])["state"], "complete")
            self.assertEqual((len(self.uploads), len(backend.mutation_calls)), (1, 1))
            self.assertEqual(backend.mutation_calls[0][3]["file_ids"], ["file-" + child_id])
        finally:
            ledger.close()

    def test_concurrent_output_creation_is_preserved(self):
        output = Path(self.directory.name) / "chosen.html"
        def competing_writer(_temporary, target):
            Path(target).write_bytes(b"another writer")
            raise FileExistsError("output created concurrently")
        with patch("murmur.backend.os.link", side_effect=competing_writer), self.assertRaises(BackendError):
            self.backend.download("actor", "post", "file", output)
        self.assertEqual(output.read_bytes(), b"another writer")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [output])

    def test_claim_and_source_buffer_are_exact(self):
        source = Path(self.directory.name) / "image.png"
        source.write_bytes(b"same-size")
        descriptor = describe_source(source, 100)
        source.write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "changed"):
            source_bytes(descriptor)

    def test_nonregular_input_is_rejected_without_waiting_for_fifo_writer(self):
        fifo = Path(self.directory.name) / "input-pipe"
        os.mkfifo(fifo, 0o600)
        result = subprocess.run([sys.executable, "-c",
            "from murmur.attachments import describe_source; import sys; describe_source(sys.argv[1], 1024)",
            str(fifo)], capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular", result.stderr)


if __name__ == "__main__":
    unittest.main()
