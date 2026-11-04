"""Canonical deletion retires attention without pretending work was read."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bookmarks as fixtures

from murmur.backend import BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError


class DeletedReadTests(unittest.TestCase):
    setUp = fixtures.BookmarkTests.setUp
    tearDown = fixtures.BookmarkTests.tearDown
    run_cli = fixtures.BookmarkTests.run_cli

    def receipt(self, post_id, actor=None):
        return dict(self.ledger.db.execute("SELECT * FROM receipts WHERE actor=? AND post_id=?",
                                          (actor or self.actor, post_id)).fetchone())

    def delete(self, post_id):
        self.backend.unavailable[post_id] = 404

    def test_deleted_oldest_is_retired_not_read_and_survives_reopen_and_replay(self):
        deleted = self.backend.add("deleted", when=101)
        self.backend.add("live", when=102)
        self.coordinator.sync(self.actor)
        self.ledger.enroll("other", {"session_id": "other"}, "Other", "other-user")
        self.ledger.sync_conversations("other", self.backend.chats)
        self.ledger.ingest("other", [deleted], 1, 1)
        self.delete("deleted")
        for _ in range(2):
            self.assertEqual([p["id"] for p in self.coordinator.read(self.actor, "build", limit=1)], ["live"])
        receipt = self.receipt("deleted")
        self.assertIsNotNone(receipt["unavailable_at"])
        self.assertIsNone(receipt["read_at"])
        self.assertIsNone(receipt["announced_at"])
        self.assertIsNone(self.receipt("deleted", "other")["unavailable_at"])
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 200)
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.ledger.ingest(self.actor, [deleted], 2, 1)
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["live"])
        self.assertEqual(self.coordinator.inbox(self.actor)[0]["unread_count"], 1)

    def test_only_deleted_post_cannot_admit_a_frozen_idle_notification(self):
        self.backend.add("deleted")
        self.coordinator.sync(self.actor)
        attempt = self.ledger.create_attempt(self.actor, "epoch")
        self.delete("deleted")
        self.assertEqual(self.coordinator.read(self.actor, "build"), [])
        self.assertEqual(self.coordinator.inbox(self.actor), [])
        with self.assertRaisesRegex(LedgerError, "No eligible frozen"):
            self.ledger.admit(attempt["token"], self.actor, "epoch", "native-turn")
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "retired")
        self.assertIsNone(self.ledger.create_attempt(self.actor, "epoch"))
        self.backend.add("later", when=103)
        self.coordinator.sync(self.actor)
        self.assertEqual(self.ledger.create_attempt(self.actor, "epoch")["post_ids"], ["later"])

    def test_output_failure_keeps_live_work_and_concurrent_arrival_unread(self):
        self.backend.add("deleted", when=101)
        self.backend.add("live", when=102)
        self.delete("deleted")
        original = self.backend.post

        def fetch(actor, post_id):
            if post_id == "live" and "concurrent" not in self.backend.canonical:
                self.backend.add("concurrent", when=103)
                self.coordinator.sync(actor)
            return original(actor, post_id)

        class BrokenOutput(io.StringIO):
            def flush(self):
                raise BrokenPipeError("Consumer disappeared")

        with patch.object(self.backend, "post", side_effect=fetch):
            with self.assertRaises(BrokenPipeError):
                self.run_cli("read", "build", "--limit", "1", output=BrokenOutput())
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["live", "concurrent"])
        result = self.run_cli("read", "build", "--limit", "1")
        self.assertEqual([p["id"] for p in result["posts"]], ["live"])
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["concurrent"])
        self.assertIsNone(self.receipt("deleted")["read_at"])

    def test_denial_and_transient_failures_do_not_retire_receipts(self):
        self.backend.add("request")
        self.coordinator.sync(self.actor)
        for status in (403, 500, None):
            with self.subTest(status=status), patch.object(self.backend, "post",
                    side_effect=BackendError("Unavailable", status=status)):
                with self.assertRaises(BackendError):
                    self.coordinator.read(self.actor, "build")
                self.assertIsNone(self.receipt("request")["unavailable_at"])
                self.assertIsNone(self.receipt("request")["read_at"])
                self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["request"])

    def test_existing_receipts_migrate_without_losing_read_or_announcement_state(self):
        self.backend.add("read")
        self.backend.add("unread", when=102)
        self.coordinator.sync(self.actor)
        self.ledger.mark_read(self.actor, ["read"])
        before = self.receipt("read")
        self.ledger.db.execute("ALTER TABLE receipts DROP COLUMN unavailable_at")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 200)
        self.assertIsNone(self.receipt("unread")["unavailable_at"])
        self.assertEqual(self.receipt("read")["read_at"], before["read_at"])
        self.assertEqual(self.receipt("read")["announced_at"], before["announced_at"])
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["unread"])

    def test_deletion_does_not_retire_or_replay_an_emitted_busy_context(self):
        self.backend.add("deleted")
        self.coordinator.sync(self.actor)
        identity = self.ledger.actor(self.actor)["identity"]
        self.ledger.start_turn(self.actor, "epoch", "working", native_identity=identity)
        attempt = self.ledger.claim_context(self.actor, "epoch", "working", "tool", native_identity=identity)
        self.ledger.mark_context_emitted(attempt["token"], self.actor, "epoch", "working", "tool")
        self.delete("deleted")
        self.assertEqual(self.coordinator.read(self.actor, "build"), [])
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "pending")
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], "working")
        self.ledger.confirm_context(attempt["token"], self.actor, "epoch", "working", "tool",
                                    verification={"native_record": "exact acceptance"})
        self.assertIsNotNone(self.receipt("deleted")["announced_at"])
        self.assertIsNone(self.receipt("deleted")["read_at"])
        self.assertTrue(self.ledger.complete(self.actor, "working", "epoch"))
        self.assertIsNone(self.ledger.create_attempt(self.actor, "epoch"))


if __name__ == "__main__":
    unittest.main()
