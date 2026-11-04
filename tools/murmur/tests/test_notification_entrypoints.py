"""Agent receipts acknowledge exact notices without consuming their requests."""

from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import unittest
from unittest.mock import patch

import test_coordinator as fixtures
from murmur import __main__ as cli, diagnostics, hooks
from murmur.backend import BackendError
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity


class NotificationEntrypointTests(unittest.TestCase):
    def setUp(self):
        fixtures.CoordinatorTests.setUp(self)
        hooks.initialize(self.ledger.db)

    tearDown = fixtures.CoordinatorTests.tearDown

    def pending(self, post="request", token="notice", when=101):
        self.backend.add(post, when=when)
        self.coordinator.sync(self.actor)
        self.ledger.create_attempt(self.actor, routing_binding(self.identity), token)
        self.ledger.set_transport(token, "uncertain", {"detail": "Native result was lost"})
        return self.ledger.attempt(token)

    def invoke(self, *arguments, output=None, inbox=None, identity=None):
        stream = io.StringIO() if output is None else output
        with patch.object(cli.config, "ledger_path", return_value=self.path), \
                patch.object(cli.config, "load", return_value={"url": "http://fixture.invalid", "token": "fixture"}), \
                patch.object(cli, "Backend", return_value=self.backend), \
                patch("murmur.native.current_identity", return_value=identity or self.identity), \
                patch.object(Coordinator, "inbox", side_effect=inbox or (lambda actor, starred_only=False: [])), \
                redirect_stdout(stream):
            cli.main(list(arguments))
        return json.loads(stream.getvalue())

    def receipts(self):
        return tuple(tuple(row) for row in self.ledger.db.execute("SELECT * FROM receipts ORDER BY arrival"))

    def test_ordinary_inbox_acknowledges_only_the_snapshot_before_its_network_work(self):
        original = self.pending()
        before = self.receipts()

        def newer_notice(actor, starred_only=False):
            self.assertEqual((actor, starred_only), (self.actor, False))
            self.pending("later", "later-notice", 102)
            return [{"id": "build", "unread_count": 2}]

        self.assertEqual(self.invoke("inbox", inbox=newer_notice), [{"id": "build", "unread_count": 2}])
        old = self.ledger.attempt(original["token"])
        later = self.ledger.attempt("later-notice")
        self.assertIsNotNone(old["agent_seen_at"])
        self.assertIsNone(later["agent_seen_at"])
        self.assertEqual(old["status"], "pending")
        self.assertEqual(old["verification"], original["verification"])
        self.assertEqual(self.receipts()[:len(before)], before)
        self.assertTrue(all(row["read_at"] is None and row["announced_at"] is None
                            for row in self.ledger.db.execute("SELECT * FROM receipts")))

    def test_starred_inbox_is_not_a_notification_acknowledgement(self):
        self.pending()
        before = tuple(self.ledger.db.iterdump())
        with patch.object(Ledger, "unseen_notice_tokens", side_effect=AssertionError("Saved view stays quiet")):
            self.assertEqual(self.invoke("inbox", "--starred"), [])
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)

    def test_failed_inbox_response_or_output_leaves_receipt_unacknowledged(self):
        self.pending()

        def failure(actor, starred_only=False):
            raise BackendError("Server unavailable")

        with self.assertRaises(BackendError):
            self.invoke("inbox", inbox=failure)
        self.assertIsNone(self.ledger.attempt("notice")["agent_seen_at"])

        class BrokenOutput:
            def write(self, value):
                raise BrokenPipeError("Model output unavailable")

            def flush(self):
                pass

        with self.assertRaises(BrokenPipeError):
            self.invoke("inbox", output=BrokenOutput())
        self.assertIsNone(self.ledger.attempt("notice")["agent_seen_at"])

        class BufferedBrokenOutput:
            def write(self, value):
                return len(value)

            def flush(self):
                raise BrokenPipeError("Buffered output was not delivered")

        with self.assertRaises(BrokenPipeError):
            self.invoke("inbox", output=BufferedBrokenOutput())
        self.assertIsNone(self.ledger.attempt("notice")["agent_seen_at"])

    def test_seen_is_local_idempotent_and_survives_reopen_without_native_admission(self):
        original = self.pending()
        before = self.receipts()

        def local_seen():
            output = io.StringIO()
            with patch.object(cli.config, "ledger_path", return_value=self.path), \
                    patch.object(cli.config, "load", side_effect=AssertionError("Receipt needs no credentials")), \
                    patch.object(cli, "Backend", side_effect=AssertionError("Receipt needs no server")), \
                    patch("murmur.native.current_identity", return_value=self.identity), redirect_stdout(output):
                cli.main(["seen", "notice"])
            return json.loads(output.getvalue())

        first = local_seen()
        self.assertFalse(first["already_seen"])
        self.ledger.close()
        self.ledger = Ledger(self.path)
        again = local_seen()
        self.assertTrue(again["already_seen"])
        self.assertEqual(again["seen_at"], first["seen_at"])
        after = self.ledger.attempt("notice")
        for field in ("status", "transport", "admitted_at", "completed_at", "verification", "post_ids", "emission"):
            self.assertEqual(after[field], original[field])
        self.assertEqual(self.receipts(), before)

    def test_seen_cannot_acknowledge_another_native_sessions_notice(self):
        self.pending()
        other = NativeIdentity("codex", "33333333-3333-4333-8333-333333333333", parent_known=True)
        self.ledger.enroll(other.actor, asdict(other), "Other", "other-user")
        before = self.ledger.attempt("notice")
        with self.assertRaises(LedgerError):
            self.invoke("seen", "notice", identity=other)
        self.assertEqual(self.ledger.attempt("notice"), before)

    def test_diagnostics_show_agent_receipt_separately_from_pending_native_evidence(self):
        self.pending()
        self.ledger.acknowledge_notices(self.actor, ["notice"])
        hooks.initialize(self.ledger.db)
        self.backend.request = lambda method, path: {"version": "fixture"}
        before = tuple(self.ledger.db.iterdump())
        with patch.object(diagnostics.native, "verify_target", return_value=self.identity), \
                patch.object(diagnostics, "route_capabilities", return_value={}):
            result = diagnostics.report(self.coordinator, self.identity, {"delivery_enabled": True})
        notice = result["attempts"][0]
        self.assertIsNotNone(notice["agent_seen_at"])
        self.assertFalse(notice["verification_present"])
        self.assertFalse(notice["receipt_blocks_new_notifications"])
        self.assertEqual(notice["status"], "pending")
        self.assertEqual(tuple(self.ledger.db.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
