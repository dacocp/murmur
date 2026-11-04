"""Independent sidechat policy with real SQLite and the existing fake HTTP boundary."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from murmur.backend import BackendError
from murmur.coordinator import Coordinator, enrollment_capabilities, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from test_coordinator import FakeBackend


SESSION = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


class SidechatBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.saved_ids = {}
        self.lose_registration = False

    def register(self, actor, name, parent, **options):
        result = dict(super().register(actor, name, parent, **options), user_id="user-" + actor)
        if self.lose_registration:
            self.lose_registration = False
            raise BackendError("Lost registration response", uncertain=True)
        return result

    def saved(self, actor):
        return [{"post_id": post_id, "post": self.post(actor, post_id),
                 "conversation": self.descriptor(actor, self.post(actor, post_id)["channel_id"])}
                for post_id in sorted(self.saved_ids.get(actor, set()))]

    def set_saved(self, actor, post_id, starred):
        saved = self.saved_ids.setdefault(actor, set())
        saved.add(post_id) if starred else saved.discard(post_id)
        return {"post_id": post_id, "starred": starred}

    def mutate(self, action, actor, op_id, **payload):
        if action != "conversation":
            return super().mutate(action, actor, op_id, **payload)
        self.mutation_calls.append((action, actor, op_id, payload))
        chat = {"id": "chat-" + op_id, "name": payload.get("name", ""),
                "kind": payload["kind"], "role": "member"}
        self.chats.append(chat)
        result = {"conversation": chat}
        self.operations[op_id] = {"status": "complete", "result": result}
        return result


class SidechatEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.addCleanup(lambda: self.ledger.close())
        self.backend = SidechatBackend()
        self.transport = Mock()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.identity = NativeIdentity("codex", SESSION)
        self.actor = self.identity.actor

    def restart(self):
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 200)
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)

    def test_unknown_ancestry_is_explicit_manual_only_with_own_account(self):
        with self.assertRaisesRegex(RuntimeError, "ancestry is unresolved"):
            self.coordinator.register(self.identity, "Normal held")
        self.assertEqual(self.backend.register_calls, [])
        self.assertEqual(self.ledger.actors(), [])
        first = self.coordinator.register(self.identity, mode="manual")
        record = self.ledger.actor(self.actor)
        self.assertEqual(record["identity"], asdict(self.identity))
        self.assertFalse(record["identity"]["parent_known"])
        self.assertIsNone(record["identity"]["parent_session_id"])
        self.assertEqual(first["enrollment"], {
            "mode": "manual", "automatic_wakeups": False, "inherited_access": False})
        self.assertEqual(self.backend.register_calls, [(self.actor, first["name"], None)])
        other = self.coordinator.register(NativeIdentity("codex", OTHER), mode="manual")
        self.assertNotEqual(first["user_id"], other["user_id"])
        self.assertNotEqual(first["name"], other["name"])
        self.assertEqual(first["user_id"], self.ledger.actor(self.actor)["user_id"])

    def test_generated_and_explicit_labels_survive_restart_without_renaming(self):
        initial = self.coordinator.register(self.identity, mode="manual")
        generated = initial["name"]
        self.assertTrue(generated.startswith("Sidechat · "))
        self.assertLessEqual(len(generated), 64)
        self.restart()
        self.assertEqual(self.coordinator.register(self.identity, mode="manual")["name"], generated)
        renamed = self.coordinator.register(self.identity, "Operator label", mode="manual")
        self.restart()
        repeated = self.coordinator.register(self.identity, mode="manual")
        self.assertEqual(repeated["name"], "Operator label")
        self.assertEqual((repeated["id"], repeated["user_id"]), (initial["id"], initial["user_id"]))
        self.assertEqual(renamed["user_id"], initial["user_id"])
        self.assertEqual(self.ledger.actor(self.actor)["enrolled_at"], 100)

    def test_modes_are_immutable_in_both_directions_and_identity_refresh(self):
        self.coordinator.register(self.identity, mode="manual")
        known = replace(self.identity, parent_known=True, parent_session_id=PARENT,
                        evidence={"parent_source": "verified later proof"})
        self.ledger.update_identity(self.actor, asdict(known))
        self.restart()
        before = self.ledger.actor(self.actor)
        calls = list(self.backend.register_calls)
        with self.assertRaisesRegex(LedgerError, "mode is immutable"):
            self.coordinator.register(known, "Do not upgrade", mode="native")
        self.assertEqual(self.ledger.actor(self.actor), before)
        self.assertEqual(self.backend.register_calls, calls)
        native = NativeIdentity("codex", OTHER, parent_known=True)
        self.coordinator.register(native, "Native session")
        native_before = self.ledger.actor(native.actor)
        with self.assertRaisesRegex(LedgerError, "mode is immutable"):
            self.coordinator.register(native, "Do not downgrade", mode="manual")
        self.assertEqual(self.ledger.actor(native.actor), native_before)

    def test_provisioning_retry_and_later_parent_proof_never_attach_parent(self):
        self.backend.lose_registration = True
        with self.assertRaisesRegex(BackendError, "Lost registration response"):
            self.coordinator.register(self.identity, "Manual child", mode="manual")
        self.assertIsNone(self.ledger.actor(self.actor)["user_id"])
        known = replace(self.identity, parent_known=True, parent_session_id=PARENT,
                        evidence={"parent_source": "verified later proof"})
        self.ledger.update_identity(self.actor, asdict(known))
        self.restart()
        self.coordinator.sync(self.actor)
        self.coordinator.register(known, "Renamed child", mode="manual")
        fresh = replace(self.identity, target={"cwd": "/fresh"}, evidence={"identity_source": "fresh env"})
        self.coordinator.register(fresh, mode="manual")
        self.assertEqual([call[2] for call in self.backend.register_calls], [None, None, None, None])
        record = self.ledger.actor(self.actor)
        self.assertEqual(record["mode"], "manual")
        self.assertEqual(record["name"], "Renamed child")
        self.assertEqual(record["identity"]["parent_session_id"], PARENT)
        self.assertEqual(record["identity"]["evidence"], {**known.evidence, **fresh.evidence})
        self.assertEqual(record["identity"]["target"], fresh.target)
        self.assertEqual(enrollment_capabilities(record["mode"])["inherited_access"], False)
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            self.coordinator.register(replace(known, parent_session_id=OTHER), "Wrong parent", mode="manual")
        self.assertEqual(self.ledger.actor(self.actor), record)
        self.assertEqual(len(self.backend.register_calls), 4)

    def test_manual_mode_rejects_provider_misuse_raw_identity_and_invalid_label(self):
        for identity in (NativeIdentity("claude", SESSION), asdict(self.identity)):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, "current Codex native identity"):
                    self.coordinator.register(identity, mode="manual")
        for name in ("", "   ", "x" * 65, False):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "nonempty name"):
                    self.coordinator.register(self.identity, name, mode="manual")
        with self.assertRaisesRegex(ValueError, "Unsupported enrollment mode"):
            self.coordinator.register(self.identity, "Bad mode", mode="unknown")
        self.assertEqual(self.ledger.actors(), [])
        self.assertEqual(self.backend.register_calls, [])

    def test_manual_refresh_preserves_verified_root_and_generated_label(self):
        initial = self.coordinator.register(self.identity, mode="manual")
        known_root = replace(self.identity, parent_known=True, evidence={"parent_source": "verified root"})
        self.ledger.update_identity(self.actor, asdict(known_root))
        result = self.coordinator.register(self.identity, mode="manual")
        self.assertEqual((result["name"], result["user_id"]), (initial["name"], initial["user_id"]))
        record = self.ledger.actor(self.actor)
        self.assertEqual(record["mode"], "manual")
        self.assertEqual(record["identity"], asdict(known_root))
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            self.coordinator.register(replace(known_root, parent_session_id=PARENT), "Wrong root", mode="manual")
        self.assertEqual(self.ledger.actor(self.actor), record)
        self.assertEqual([call[2] for call in self.backend.register_calls], [None, None])

    def test_manual_arrivals_never_allocate_queue_or_busy_attempts(self):
        self.coordinator.register(self.identity, mode="manual")
        self.backend.add("request", causal="manual-exchange")
        self.coordinator.sync(self.actor)
        unread = self.ledger.unread_posts(self.actor)
        self.assertEqual([post["id"] for post in unread], ["request"])
        self.assertTrue(unread[0]["notification_eligible"])
        binding = routing_binding(self.identity)
        self.assertIsNone(self.ledger.create_attempt(self.actor, binding))
        self.ledger.start_turn(self.actor, binding, "codex:actual-turn", native_identity=asdict(self.identity))
        self.assertIsNone(self.ledger.claim_context(self.actor, binding, "codex:actual-turn", "actual-tool",
                                                  native_identity=asdict(self.identity)))
        self.assertIsNone(self.coordinator.deliver(self.identity))
        self.assertEqual(self.coordinator.reconcile_context(self.actor), [])
        self.assertEqual(self.transport.mock_calls, [])
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertEqual(self.ledger.unread_posts(self.actor), unread)

    def test_manual_chat_read_history_reply_and_saves_need_no_native_resolution(self):
        self.coordinator.register(self.identity, mode="manual")
        dm = self.coordinator.mutate(self.actor, "conversation", kind="dm", members=["codex:peer"])
        channel = self.coordinator.mutate(self.actor, "conversation", kind="channel", name="Work", members=["codex:peer"])
        self.assertEqual((dm["conversation"]["kind"], channel["conversation"]["kind"]), ("dm", "channel"))
        self.backend.add("question")
        self.backend.add("reply", root="question", when=102)
        inbox = self.coordinator.inbox(self.actor)
        self.assertEqual(inbox[0]["unread_count"], 2)
        read = self.coordinator.read(self.actor, "build", root="question", limit=1)
        self.assertEqual([post["id"] for post in read], ["question"])
        self.ledger.mark_read(self.actor, ["question"])
        self.coordinator.set_saved(self.actor, "question", True)
        saved = self.coordinator.inbox(self.actor, starred_only=True)
        self.assertEqual(saved[0]["starred"][0]["post_id"], "question")
        self.assertEqual(self.coordinator.show(self.actor, "question")["id"], "question")
        self.assertEqual(len(self.coordinator.history(self.actor, "build", root="question")["posts"]), 2)
        self.coordinator.mutate(self.actor, "post", channel_id="build", root_id="question", message="Useful reply")
        self.assertEqual(self.backend.mutation_calls[-1][1], self.actor)
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["reply"])
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertFalse(self.ledger.actor(self.actor)["identity"]["parent_known"])

    def test_legacy_actor_migration_preserves_native_policy_and_account(self):
        path = self.path.parent / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE actors(actor TEXT PRIMARY KEY,name TEXT NOT NULL,identity_json TEXT NOT NULL,
            user_id TEXT,enrolled_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,
            generation INTEGER,causal_json TEXT NOT NULL DEFAULT '[]')""")
        db.execute("INSERT INTO actors VALUES(?,?,?,?,?,?,?,?,?)",
                   (self.actor, "Existing native", json.dumps(asdict(self.identity)), "existing-user", 10, 20, 7, 2, '[]'))
        db.commit()
        db.close()
        migrated = Ledger(path)
        try:
            record = migrated.actor(self.actor)
            self.assertEqual((record["mode"], record["name"], record["user_id"], record["cursor"]),
                             ("native", "Existing native", "existing-user", 7))
            with self.assertRaises(sqlite3.IntegrityError):
                migrated.db.execute("UPDATE actors SET mode='unknown' WHERE actor=?", (self.actor,))
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
