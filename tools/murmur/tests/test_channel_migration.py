"""Schema-2 fixtures retain frozen handoffs, native state and immutable intents."""

from contextlib import closing, redirect_stderr
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from murmur import __main__ as cli
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from test_open_channels import OpenBackend


class ChannelMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="murmur-channel-migration-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "schema2.sqlite3"
        self.backend = OpenBackend()
        self.backend.chats.append(dict(id="review-group", name="Existing group title", kind="channel", role="member"))
        self.parent = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
        self.child = NativeIdentity("codex", "22222222-2222-4222-8222-222222222222", self.parent.session_id, True)
        self.join_id = "inherit-" + hashlib.sha256((self.child.key + "\0review-group").encode()).hexdigest()[:40]
        # Schema 3 changes data spelling only. Seed the unchanged table layout,
        # then record explicit schema-2 data rather than derive it from migration.
        with closing(Ledger(self.path, clock=lambda: 100)) as ledger:
            runtime = Coordinator(ledger, self.backend)
            runtime.register(self.parent, "Parent group title")
            runtime.register(self.child, "Child")
            self.backend.add("request-group", channel="dm")
            self.backend.add("saved", when=102)
            runtime.sync(self.parent.key)
            ledger.mark_read(self.parent.key, ["saved"])
            self.backend.set_saved(self.parent.key, "saved", True)
            epoch = routing_binding(self.parent)
            ledger.create_attempt(self.parent.key, epoch, "preserve-native")
            ledger.admit("preserve-native", self.parent.key, epoch, "native-turn", native_identity=asdict(self.parent))
            ledger.set_transport("preserve-native", "accepted", {"receipt": "preserve group text"})
            # A completed inherited join was later left, and its explicit mute
            # was changed. Another join took effect but lost its response.
            ledger.set_mute(self.child.key, "build", False)
            del self.backend.roles[self.child.key]["build"]
            self.backend.roles[self.child.key]["review-group"] = "member"
            runtime.sync(self.child.key)
            self.backend.operations[self.join_id] = {"conversation": self.backend.descriptor(self.child.key, "review-group")}
            ledger.prepare_outbound(self.child.key, {"action": "join", "channel_id": "review-group"}, self.join_id)
            ledger.set_outbound(self.join_id, "uncertain", error="Legacy response lost")
            ledger.prepare_outbound(self.child.key, {"action": "conversation"}, "old-create")
            self.old_intent = '{ "members": [], "kind": "group", "name": "Old group title", "action": "conversation" }'
            ledger.db.execute("UPDATE outbound SET payload_json=? WHERE op_id='old-create'", (self.old_intent,))
            ledger.set_outbound("old-create", "uncertain", error="Legacy creation response lost")
            self.backend.operations["old-create"] = {"conversation": {"id": "old-created-channel"}}
            plan = ledger.onboarding(self.child.key)
            self.plan_id = plan["plan_id"]
            self.snapshot = {"unread": [{"id": "request-group", "channel_id": "dm", "user_id": "peer", "root_id": ""}],
                "groups": [{"id": "build", "muted": 1}, {"id": "review-group", "muted": 1}],
                "saved_ids": ["saved"], "local_captured_at": 71, "saved_started_at": 72,
                "saved_finished_at": 73, "note": "groups in names are unchanged"}
            self.progress = {"post:request-group": "done", "star:saved": "started",
                             "group:build": "done", "group:review-group": "started"}
            ledger.db.execute("""UPDATE onboarding SET choice='pending-and-groups',source_actor=?,
                snapshot_json=?,progress_json=?,status='pending',error='Legacy held save',created_at=70,updated_at=74
                WHERE actor=?""", (self.parent.key, json.dumps(self.snapshot), json.dumps(self.progress), self.child.key))
            ledger.db.execute("""INSERT INTO receipts(actor,post_id,read_at,announced_at,notification_eligible)
                VALUES(?, 'request-group', 81, 82, 0)""", (self.child.key,))
            ledger.db.execute("UPDATE conversations SET kind='group' WHERE kind='channel'")
            ledger.db.execute("PRAGMA user_version=2")
            self.before = self.tables(ledger.db)

    @staticmethod
    def tables(db):
        db.row_factory = sqlite3.Row
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: sorted((dict(row) for row in db.execute('SELECT * FROM "' + name + '"')), key=repr)
                for name in names}

    def test_schema_two_names_migrate_once_and_other_work_state_is_exact(self):
        with closing(Ledger(self.path, clock=lambda: 999)) as ledger:
            after = self.tables(ledger.db)
            self.assertEqual(ledger.db.execute("PRAGMA user_version").fetchone()[0], 4)
            for name in self.before.keys() - {"conversations", "onboarding"}:
                self.assertEqual(after[name], self.before[name], name)
            expected_chats = [dict(row, kind="channel" if row["kind"] == "group" else row["kind"])
                              for row in self.before["conversations"]]
            self.assertEqual(after["conversations"], expected_chats)
            self.assertTrue(any(not row["available"] and row["kind"] == "channel" for row in after["conversations"]))
            before_plan = next(row for row in self.before["onboarding"] if row["actor"] == self.child.key)
            after_plan = next(row for row in after["onboarding"] if row["actor"] == self.child.key)
            for field in before_plan.keys() - {"choice", "snapshot_json", "progress_json"}:
                self.assertEqual(after_plan[field], before_plan[field], field)
            self.assertEqual(after_plan["choice"], "pending-and-channels")
            expected_snapshot = dict(self.snapshot)
            expected_snapshot["channels"] = expected_snapshot.pop("groups")
            self.assertEqual(json.loads(after_plan["snapshot_json"]), expected_snapshot)
            self.assertEqual(json.loads(after_plan["progress_json"]), {"post:request-group": "done", "star:saved": "started",
                "channel:build": "done", "channel:review-group": "started"})
            self.assertEqual(ledger.db.execute("SELECT payload_json FROM outbound WHERE op_id='old-create'").fetchone()[0], self.old_intent)
        with closing(Ledger(self.path)) as ledger:
            self.assertEqual(self.tables(ledger.db), after)

    def test_held_save_and_started_join_resume_the_same_frozen_plan_without_recopying(self):
        star_calls, parent_calls = list(self.backend.star_calls), self.backend.change_calls.count(self.parent.key)
        with closing(Ledger(self.path, clock=lambda: 200)) as ledger:
            runtime = Coordinator(ledger, self.backend)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                runtime.register(self.child, "Child", inherit="pending-and-channels")
            plan = ledger.onboarding(self.child.key)
            self.assertEqual((plan["plan_id"], plan["progress"]["star:saved"]), (self.plan_id, "started"))
            self.assertFalse(ledger.onboarding_ready(self.child.key))
            self.assertEqual(self.backend.mutation_calls, [])
            runtime.recover_onboarding(self.child.key, self.plan_id, "star:saved", "Explicit fixture skip")
            runtime.register(self.child, "Child")
            plan = ledger.onboarding(self.child.key)
            self.assertEqual((plan["plan_id"], plan["status"], plan["choice"]),
                             (self.plan_id, "complete", "pending-and-channels"))
            self.assertEqual(plan["snapshot"]["local_captured_at"], 71)
            self.assertEqual((plan["snapshot"]["saved_started_at"], plan["snapshot"]["saved_finished_at"]), (72, 73))
            self.assertEqual(plan["progress"]["star:saved"], "skipped")
            self.assertEqual((plan["progress"]["channel:build"], plan["progress"]["channel:review-group"]), ("done", "done"))
            self.assertEqual(self.backend.operation_calls, [self.join_id])
            self.assertEqual(self.backend.mutation_calls, [])
            self.assertEqual(ledger.outbound(self.join_id)["state"], "complete")
            left = ledger.db.execute("SELECT available,muted,mute_explicit FROM conversations WHERE actor=? AND id='build'",
                                     (self.child.key,)).fetchone()
            self.assertEqual(tuple(left), (0, 0, 1))
            self.assertEqual(self.backend.star_calls, star_calls)
            self.assertEqual(self.backend.change_calls.count(self.parent.key), parent_calls)

    def test_legacy_uncertain_creation_reconciles_by_id_without_rewriting_or_reposting(self):
        with closing(Ledger(self.path)) as ledger:
            runtime = Coordinator(ledger, self.backend)
            runtime.reconcile_outbound(self.child.key)
            runtime.reconcile_outbound(self.child.key)
            self.assertEqual((ledger.outbound("old-create")["state"], ledger.outbound("old-create")["result"]),
                             ("complete", {"channel_id": "old-created-channel"}))
            self.assertEqual(ledger.db.execute("SELECT payload_json FROM outbound WHERE op_id='old-create'").fetchone()[0], self.old_intent)
            ledger.prepare_outbound(self.child.key, {"action": "conversation", "kind": "group"}, "missing-old")
            ledger.set_outbound("missing-old", "uncertain", error="Unknown legacy result")
            runtime.reconcile_outbound(self.child.key)
            runtime.reconcile_outbound(self.child.key)
            self.assertEqual(ledger.outbound("missing-old")["state"], "uncertain")
            self.assertEqual(self.backend.operation_calls.count("old-create"), 1)
            self.assertEqual(self.backend.operation_calls.count("missing-old"), 2)
            self.assertEqual(self.backend.mutation_calls, [])

    def test_conflicting_frozen_keys_or_write_fault_roll_back_names_and_schema(self):
        for fault in ("snapshot", "progress", "write"):
            with self.subTest(fault=fault):
                path = self.path.with_name(fault + ".sqlite3")
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(path)) as db:
                    source.backup(db)
                    if fault == "snapshot":
                        conflicting = dict(self.snapshot, channels=[{"id": "different", "muted": 0}])
                        db.execute("UPDATE onboarding SET snapshot_json=? WHERE actor=?", (json.dumps(conflicting), self.child.key))
                    elif fault == "progress":
                        conflicting = dict(self.progress, **{"channel:build": "started"})
                        db.execute("UPDATE onboarding SET progress_json=? WHERE actor=?", (json.dumps(conflicting), self.child.key))
                    else:
                        db.execute("""CREATE TRIGGER migration_fault BEFORE UPDATE OF snapshot_json ON onboarding
                            BEGIN SELECT RAISE(ABORT, 'fixture migration write fault'); END""")
                    db.commit()
                    before = self.tables(db)
                connections, connect = [], sqlite3.connect
                def capture(*args, **options):
                    connection = connect(*args, **options)
                    connections.append(connection)
                    return connection
                with patch("murmur.ledger.sqlite3.connect", side_effect=capture):
                    with self.assertRaisesRegex(sqlite3.IntegrityError if fault == "write" else LedgerError,
                                                "fixture migration" if fault == "write" else "Conflicting"):
                        Ledger(path)
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    connections[0].execute("SELECT 1")
                with closing(connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                    self.assertEqual(self.tables(db), before)

    def test_old_public_commands_and_inherit_choice_fail_before_any_external_work(self):
        with patch.object(cli, "Ledger", side_effect=AssertionError("Old aliases must not open state")), \
                patch.object(cli.config, "load", side_effect=AssertionError("Old aliases must not read config")), \
                patch("murmur.native.current_identity", side_effect=AssertionError("Old aliases must not inspect native state")), \
                redirect_stderr(io.StringIO()):
            for arguments in (["group", "Title"], ["groups"],
                              ["register", "--name", "Child", "--inherit", "pending-and-groups"]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                    cli.main(arguments)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
