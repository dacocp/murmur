from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur.ledger import Ledger, LedgerError
from murmur.coordinator import Coordinator


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.path = Path(self.home.name) / "ledger.sqlite3"
        self.now = 100
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.ledger.enroll("a", {"session_id": "a", "parent": None}, "Agent A", "user-a")
        self.chat("build")
        self.sequence = 0

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def chat(self, channel, *, role="member", kind="channel"):
        chats = self.ledger.conversations("a")
        chats = [c for c in chats if c["id"] != channel]
        chats.append({"id": channel, "name": channel.title(), "kind": kind, "role": role})
        self.ledger.sync_conversations("a", chats)

    def post(self, post_id, *, channel="build", when=101, user="user-b", causal=None, **extra):
        result = {"id": post_id, "channel_id": channel, "user_id": user, "author": "Agent B",
                  "message": "BODY SHOULD NOT ENTER SQLITE " + post_id,
                  "root_id": "", "create_at": when, "update_at": when, **extra}
        if causal is not None:
            result["causal"] = causal
        return result

    def ingest(self, *posts, generation=1):
        self.sequence += 1
        self.ledger.ingest("a", posts, self.sequence, generation)

    def test_idle_evidence_refresh_is_cas_bound_and_does_not_rebind_existing_handoff(self):
        self.ledger.update_identity("a", {"provider": "codex", "session_id": "a", "parent": None})
        old = self.ledger.actor("a")["identity"]
        fresh = dict(old, target={"transport": "queue"}, evidence={"desktop_wake": {"owner": "successor"}})
        self.ingest(self.post("request"))
        attempt = self.ledger.create_attempt("a", "epoch", "original", expected_identity=old,
                                             refreshed_identity=fresh)
        self.assertEqual(attempt["native_identity"], fresh)
        self.assertEqual(self.ledger.actor("a")["identity"], fresh)
        self.assertIsNone(self.ledger.unread_posts("a")[0]["announced_at"])
        later = dict(fresh, evidence={"desktop_wake": {"owner": "another-generation"}})
        self.assertIsNone(self.ledger.create_attempt("a", "epoch", expected_identity=fresh,
                                                    refreshed_identity=later))
        self.assertEqual(self.ledger.attempt("original"), attempt)
        self.assertEqual(self.ledger.actor("a")["identity"], fresh)

    def test_idle_evidence_refresh_cannot_change_ancestry(self):
        self.ledger.update_identity("a", {"provider": "codex", "session_id": "a", "parent": None})
        old = self.ledger.actor("a")["identity"]
        self.ingest(self.post("request"))
        with self.assertRaises(LedgerError):
            self.ledger.create_attempt("a", "epoch", expected_identity=old,
                refreshed_identity=dict(old, parent="different", target={}, evidence={"new": True}))
        self.assertEqual(self.ledger.actor("a")["identity"], old)
        self.assertEqual(self.ledger.attempts("a"), [])
        with self.assertRaises(LedgerError):
            self.ledger.create_attempt("a", "epoch", expected_identity=old,
                refreshed_identity=dict(old, target={}, evidence={"parent_proof": "changed"}))
        self.assertEqual(self.ledger.actor("a")["identity"], old)
        self.assertEqual(self.ledger.attempts("a"), [])

    def test_future_schema_is_refused_without_persistent_mutation_or_retained_connection(self):
        for journal in ("delete", "wal"):
            with self.subTest(journal=journal):
                directory = Path(self.home.name) / ("future-" + journal)
                directory.mkdir()
                path = directory / "operations.sqlite3"
                with closing(sqlite3.connect(path, isolation_level=None)) as writer:
                    writer.execute("PRAGMA journal_mode=" + journal)
                    writer.executescript("""PRAGMA user_version=5;
                        CREATE TABLE future_only(id INTEGER PRIMARY KEY, marker TEXT NOT NULL);
                        INSERT INTO future_only VALUES(1,'preserve future schema');""")
                    # SQLite readers update shared-memory read marks even in
                    # read-only mode. Preserve the database/WAL bytes and sidecar
                    # names; cache coordination bytes are not persistent state.
                    def persistent_files():
                        return {file.name: file.read_bytes() for file in directory.iterdir()
                                if not file.name.endswith("-shm")}

                    if journal == "wal":
                        self.assertGreater((directory / (path.name + "-wal")).stat().st_size, 0)
                    before = persistent_files()
                    before_names = sorted(file.name for file in directory.iterdir())
                    connections = []
                    connect = sqlite3.connect

                    def capture(*args, **kwargs):
                        connection = connect(*args, **kwargs)
                        connections.append(connection)
                        return connection

                    with patch("murmur.ledger.sqlite3.connect", side_effect=capture):
                        with self.assertRaisesRegex(LedgerError, "schema 5 is newer than supported schema 4"):
                            Ledger(path)
                    self.assertEqual(persistent_files(), before)
                    self.assertEqual(sorted(file.name for file in directory.iterdir()), before_names)
                    self.assertEqual(len(connections), 1)
                    with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                        connections[0].execute("SELECT 1")
                    db = connect(path, isolation_level=None)
                    try:
                        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
                        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], journal)
                        self.assertEqual(db.execute("SELECT * FROM future_only").fetchall(), [(1, "preserve future schema")])
                        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("future_only",)])
                    finally:
                        db.close()
                    self.assertEqual(persistent_files(), before)
                    self.assertEqual(sorted(file.name for file in directory.iterdir()), before_names)

    def test_schema_zero_and_one_upgrade_preserves_existing_actor_and_rows(self):
        for version in (0, 1):
            with self.subTest(version=version):
                path = Path(self.home.name) / ("legacy-" + str(version) + ".sqlite3")
                db = sqlite3.connect(path, isolation_level=None)
                db.executescript("""CREATE TABLE actors (
                    actor TEXT PRIMARY KEY,name TEXT NOT NULL,identity_json TEXT NOT NULL,
                    user_id TEXT,enrolled_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                    cursor INTEGER NOT NULL DEFAULT 0,generation INTEGER,
                    causal_json TEXT NOT NULL DEFAULT '[]');
                    INSERT INTO actors VALUES('legacy','Legacy reader','{}','legacy-account',100,101,7,2,'[]');
                    CREATE TABLE legacy_only(id INTEGER PRIMARY KEY,marker TEXT NOT NULL);
                    INSERT INTO legacy_only VALUES(1,'preserve legacy rows');""")
                db.execute("PRAGMA user_version=" + str(version))
                old_actor = db.execute("SELECT * FROM actors").fetchone()
                db.close()
                ledger = Ledger(path)
                try:
                    self.assertEqual(ledger.db.execute("PRAGMA user_version").fetchone()[0], 4)
                    retained = ledger.db.execute("""SELECT actor,name,identity_json,user_id,enrolled_at,
                        updated_at,cursor,generation,causal_json FROM actors""").fetchone()
                    self.assertEqual(tuple(retained), old_actor)
                    self.assertEqual(tuple(ledger.db.execute("SELECT * FROM legacy_only").fetchone()),
                                     (1, "preserve legacy rows"))
                    actor = ledger.actor("legacy")
                    self.assertEqual(actor["enrollment_seq"], 0)
                    self.assertIsNone(actor["enrollment_id"])
                finally:
                    ledger.close()

    def test_explicit_enrollment_is_required_and_provisioning_is_independent(self):
        with self.assertRaisesRegex(LedgerError, "explicitly enrolled"):
            self.ledger.ingest("unrelated", [], 0, 1)
        self.now = 150
        self.ledger.update_identity("a", {"session_id": "a", "epoch": "new"})
        self.assertEqual(self.ledger.actor("a")["enrolled_at"], 100)
        self.assertEqual(self.ledger.actor("a")["name"], "Agent A")
        with self.assertRaisesRegex(LedgerError, "cannot change"):
            self.ledger.set_user_id("a", "someone-else")
        with self.assertRaisesRegex(LedgerError, "cannot change"):
            self.ledger.enroll("a", {}, "Agent A", "someone-else")
        self.assertEqual(len(self.ledger.actors()), 1)

    def test_transport_response_after_admission_retains_both_separate_facts(self):
        self.ingest(self.post("fast-delivery"))
        self.ledger.create_attempt("a", "epoch", "fast-token")
        self.ledger.admit("fast-token", "a", "epoch", "actual-turn")
        self.ledger.complete("a", "actual-turn", "epoch")
        self.ledger.set_transport("fast-token", "accepted", {"receipt": "matching"})
        attempt = self.ledger.attempt("fast-token")
        self.assertEqual(attempt["status"], "completed")
        self.assertEqual(attempt["transport"], "accepted")
        self.assertEqual(attempt["evidence"], {"receipt": "matching"})
        self.assertIsNotNone(self.ledger.unread_posts("a")[0]["announced_at"])

    def test_enrollment_and_target_refresh_preserve_verified_native_identity(self):
        actor = "codex:11111111-1111-4111-8111-111111111111"
        known = {"provider": "codex", "session_id": actor.split(":")[1], "parent_known": True,
                 "parent_session_id": "22222222-2222-4222-8222-222222222222", "target": {"pid": 41}}
        self.ledger.enroll(actor, known, "Known child", "child-user")
        before = self.ledger.actor(actor)
        changes = ({"provider": "claude"}, {"session_id": "33333333-3333-4333-8333-333333333333"},
                   {"parent_session_id": None}, {"parent_session_id": "44444444-4444-4444-8444-444444444444"},
                   {"parent_known": False})
        for changed in changes:
            with self.subTest(changed=changed):
                refreshed = dict(known, **changed)
                with self.assertRaisesRegex(LedgerError, "immutable"):
                    self.ledger.enroll(actor, refreshed, "Must not replace name")
                self.assertEqual(self.ledger.actor(actor), before)
                with self.assertRaisesRegex(LedgerError, "immutable"):
                    self.ledger.update_identity(actor, refreshed)
                self.assertEqual(self.ledger.actor(actor), before)
        self.ledger.update_identity(actor, dict(known, target={"pid": 42}))
        self.assertEqual(self.ledger.actor(actor)["identity"]["target"], {"pid": 42})

    def test_unknown_parent_can_be_verified_once_but_known_root_cannot_be_reparented(self):
        unknown = {"provider": "codex", "session_id": "a", "parent_known": False, "parent_session_id": None}
        self.ledger.enroll("a", unknown, "Pending proof")
        root = dict(unknown, parent_known=True)
        self.ledger.update_identity("a", root)
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            self.ledger.enroll("a", dict(root, parent_session_id="parent"), "Rejected reparent")
        self.assertEqual(self.ledger.actor("a")["identity"], root)
        self.assertEqual(self.ledger.actor("a")["name"], "Pending proof")
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            self.ledger.update_identity("a", unknown)

    def test_reparent_registration_rejects_before_backend_and_preserves_local_record(self):
        identity = {"provider": "codex", "session_id": "11111111-1111-4111-8111-111111111111",
                    "parent_known": True, "parent_session_id": None, "target": {"pid": 41}}
        actor = "codex:" + identity["session_id"]
        self.ledger.enroll(actor, identity, "Established root", "known-user")
        before = self.ledger.actor(actor)
        backend = Mock()
        coordinator = Coordinator(self.ledger, backend)
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            coordinator.register(dict(identity, parent_session_id="22222222-2222-4222-8222-222222222222"),
                                 "Should not replace established name")
        self.assertEqual(backend.mock_calls, [])
        self.assertEqual(self.ledger.actor(actor), before)

    def test_invalid_native_lineage_cannot_announce_or_replace_an_active_turn(self):
        known = {"provider": "codex", "session_id": "a", "parent_known": True, "parent_session_id": None}
        self.ledger.enroll("a", known, "Known root")
        self.ingest(self.post("pending"))
        self.ledger.create_attempt("a", "epoch", "wake")
        self.ledger.start_turn("a", "epoch", "existing", native_identity=known)
        reparented = dict(known, parent_session_id="different-parent")
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            self.ledger.admit("wake", "a", "epoch", "new", supersede=True, native_identity=reparented)
        with self.assertRaisesRegex(LedgerError, "ancestry is immutable"):
            self.ledger.start_turn("a", "epoch", "new", supersede=True, native_identity=reparented)
        self.assertEqual(self.ledger.active_turn("a")["turn_key"], "existing")
        self.assertEqual(self.ledger.attempt("wake")["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts("a")[0]["announced_at"])

    def test_inbox_and_limited_reads_preserve_concurrent_arrivals_and_threads(self):
        self.ingest(self.post("p1"), self.post("p2", when=102, root_id="p1"),
                    self.post("other", when=103))
        before = self.ledger.inbox("a")
        self.assertEqual(before[0]["unread_count"], 3)
        selected = self.ledger.unread_posts("a", "build", root_id="p1", limit=1)
        self.assertEqual([p["id"] for p in selected], ["p1"])
        second_connection = Ledger(self.path)
        try:
            second_connection.ingest("a", [self.post("later", when=104)], 2, 1)
        finally:
            second_connection.close()
        self.ledger.mark_read("a", [p["id"] for p in selected])
        self.assertEqual([p["id"] for p in self.ledger.unread_posts("a")], ["p2", "other", "later"])
        self.assertEqual(self.ledger.inbox("a")[0]["unread_count"], 3)
        self.assertEqual(before[0]["unread_count"], 3)  # listing itself never consumed
        raw = self.ledger.db.execute("SELECT * FROM posts WHERE id='p1'").fetchone()
        self.assertNotIn("message", raw.keys())

    def test_duplicate_reordered_updates_restart_and_cursor_are_atomic(self):
        self.ingest(self.post("p2", when=102), self.post("p1", when=101))
        self.ledger.mark_read("a", ["p1"])
        self.ingest(self.post("p1", update_at=110), self.post("p2", when=102))
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual([p["id"] for p in self.ledger.unread_posts("a")], ["p2"])
        self.assertEqual(self.ledger.cursor("a"), {"cursor": 2, "generation": 1})
        with self.assertRaisesRegex(LedgerError, "identity changed"):
            self.ledger.ingest("a", [self.post("new"), self.post("p1", channel="wrong")], 3, 1)
        self.assertEqual(self.ledger.cursor("a")["cursor"], 2)
        self.assertIsNone(self.ledger.db.execute("SELECT id FROM posts WHERE id='new'").fetchone())
        self.ledger.ingest("a", [self.post("p2", when=102)], 0, 2)
        self.assertEqual(self.ledger.cursor("a"), {"cursor": 0, "generation": 2})
        with self.assertRaisesRegex(LedgerError, "backwards"):
            self.ledger.ingest("a", [], -1, 2)

    def test_all_muted_mail_stays_unread_including_dms_mentions_and_observers(self):
        self.chat("private", kind="dm")
        self.chat("observer", role="observer")
        self.ledger.set_mute("a", "build", True)
        self.ledger.set_mute("a", "private", True)
        self.ingest(self.post("channel", mentions=["a"]), self.post("dm", channel="private"),
                    self.post("observer", channel="observer"))
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.assertEqual(sum(c["unread_count"] for c in self.ledger.inbox("a")), 3)
        self.ledger.set_mute("a", "observer", False)
        self.chat("observer", role="member")
        observer = next(c for c in self.ledger.conversations("a") if c["id"] == "observer")
        self.assertFalse(observer["muted"])
        self.assertTrue(observer["mute_explicit"])
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("new-observer", channel="observer", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch")["post_ids"], ["new-observer"])

    def test_join_enables_future_messages_without_replaying_observer_history(self):
        self.chat("observer", role="observer")
        self.ingest(self.post("before-join", channel="observer"))
        self.chat("observer", role="member")
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("before-join", channel="observer"),
                    self.post("after-join", channel="observer", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch")["post_ids"], ["after-join"])
        self.assertEqual(self.ledger.inbox("a")[0]["unread_count"], 2)
        before = next(p for p in self.ledger.unread_posts("a") if p["id"] == "before-join")
        self.assertIsNone(before["announced_at"])

    def test_unmuting_dm_only_enables_future_arrivals_even_after_restart_and_replay(self):
        self.chat("private", kind="dm")
        self.ledger.set_mute("a", "private", True)
        self.ingest(self.post("muted-dm", channel="private"))
        self.ledger.set_mute("a", "private", False)
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("muted-dm", channel="private"),
                    self.post("new-dm", channel="private", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch")["post_ids"], ["new-dm"])
        self.assertEqual(self.ledger.inbox("a")[0]["unread_count"], 2)

    def test_muting_after_scheduling_does_not_revive_the_old_doorbell_on_unmute(self):
        self.ingest(self.post("scheduled"))
        self.ledger.create_attempt("a", "epoch", "old-wake")
        self.ledger.set_mute("a", "build", True)
        self.ledger.set_mute("a", "build", False)
        with self.assertRaisesRegex(LedgerError, "No eligible"):
            self.ledger.admit("old-wake", "a", "epoch", "old-turn")
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("new", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch")["post_ids"], ["new"])

    def test_unknown_channel_arrival_and_departed_backlog_do_not_wake_on_join(self):
        self.ingest(self.post("unknown", channel="not-yet-known"))
        self.chat("not-yet-known")
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("unannounced"))
        self.ledger.sync_conversations("a", [])
        self.chat("build")
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.assertEqual([p["id"] for p in self.ledger.unread_posts("a")], ["unannounced"])

    def test_prior_schema_retains_unread_without_replaying_unproved_attention(self):
        self.ingest(self.post("existing"))
        self.ledger.db.execute("ALTER TABLE receipts DROP COLUMN notification_eligible")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual([p["id"] for p in self.ledger.unread_posts("a")], ["existing"])
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("new", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch")["post_ids"], ["new"])

    def test_member_defaults_change_only_without_an_explicit_preference(self):
        self.chat("observer", role="observer")
        self.chat("observer", role="member")
        self.assertFalse(next(c for c in self.ledger.conversations("a") if c["id"] == "observer")["muted"])
        self.ledger.set_mute("a", "observer", True)
        self.chat("observer", role="observer")
        self.chat("observer", role="member")
        self.assertTrue(next(c for c in self.ledger.conversations("a") if c["id"] == "observer")["muted"])

    def test_admission_freezes_batch_not_concurrent_arrivals_or_queue_acceptance(self):
        self.ingest(self.post("first"))
        attempt = self.ledger.create_attempt("a", "epoch", "wake-1")
        self.ledger.set_transport("wake-1", "accepted", {"queue_id": "native-q"})
        self.assertEqual(self.ledger.unread_posts("a")[0]["announced_at"], None)
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("later", when=102))
        self.assertTrue(self.ledger.admit("wake-1", "a", "epoch", "turn-1"))
        self.assertFalse(self.ledger.admit("wake-1", "a", "epoch", "turn-1"))
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.assertFalse(self.ledger.complete("a", "stale-turn"))
        self.assertTrue(self.ledger.complete("a", "turn-1", "epoch"))
        self.assertEqual(self.ledger.create_attempt("a", "epoch", "wake-2")["post_ids"], ["later"])
        self.assertEqual(attempt["post_ids"], ["first"])

    def test_ignored_backlog_never_rings_again_but_later_arrivals_do(self):
        self.ingest(self.post("backlog"))
        self.ledger.create_attempt("a", "epoch", "wake-1")
        self.ledger.admit("wake-1", "a", "epoch", "turn-1")
        self.ledger.complete("a", "turn-1")
        self.assertEqual(self.ledger.inbox("a")[0]["unread_count"], 1)
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("new", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch")["post_ids"], ["new"])

    def test_old_history_own_posts_and_system_events_never_wake(self):
        # Freshness is a canonical enrollment boundary, never host wall time.
        self.ledger.db.execute("UPDATE actors SET enrollment_seq=1 WHERE actor='a'")
        self.ingest(self.post("old", when=99), self.post("own", user="user-a"),
                    self.post("system", type="system_join_channel"))
        self.assertEqual([p["id"] for p in self.ledger.unread_posts("a")], ["old"])
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))

    def test_wrong_recipient_epoch_replay_and_late_muting_fail_closed(self):
        self.ingest(self.post("first"))
        self.ledger.create_attempt("a", "epoch", "wake")
        for actor, epoch in (("other", "epoch"), ("a", "retired-epoch")):
            with self.assertRaisesRegex(LedgerError, "does not match"):
                self.ledger.admit("wake", actor, epoch, "turn")
        self.ledger.set_mute("a", "build", True)
        with self.assertRaisesRegex(LedgerError, "No eligible"):
            self.ledger.admit("wake", "a", "epoch", "turn")
        self.assertEqual(self.ledger.attempt("wake")["status"], "retired")
        with self.assertRaisesRegex(LedgerError, "retired"):
            self.ledger.admit("wake", "a", "epoch", "turn")
        self.assertIsNone(self.ledger.unread_posts("a")[0]["announced_at"])

    def test_rejection_can_reschedule_uncertainty_cannot_and_busy_coalesces(self):
        self.ingest(self.post("first"))
        self.ledger.start_turn("a", "epoch", "human")
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.ingest(self.post("second", when=102))
        self.assertFalse(self.ledger.complete("a", "human", "wrong-epoch"))
        self.ledger.complete("a", "human", "epoch")
        self.assertEqual(self.ledger.create_attempt("a", "epoch", "rejected")["post_ids"], ["first", "second"])
        self.ledger.set_transport("rejected", "rejected")
        self.ledger.create_attempt("a", "epoch", "uncertain")
        self.ledger.set_transport("uncertain", "uncertain")
        self.assertIsNone(self.ledger.create_attempt("a", "epoch"))
        self.assertEqual([a["token"] for a in self.ledger.pending_attempts()], ["uncertain"])
        self.ledger.retire_attempt("uncertain", "target verified gone")
        self.assertIsNone(self.ledger.create_attempt("a", "new-epoch"))
        self.ingest(self.post("later", when=103))
        self.assertEqual(self.ledger.create_attempt("a", "new-epoch")["post_ids"], ["later"])

    def test_active_owner_snapshot_survives_target_update_and_stale_exit_result(self):
        old = {"session_id": "a", "target": {"pid": 41, "native_epoch": "old-process"}}
        new = {"session_id": "a", "target": {"pid": 42, "native_epoch": "new-process"}}
        self.ledger.update_identity("a", old)
        self.ledger.start_turn("a", "old-epoch", "old-turn")
        old_active = self.ledger.active_turn("a")
        self.ledger.enroll("a", new, "Renamed agent")
        self.assertEqual(self.ledger.active_turn("a")["identity"], old)
        self.assertEqual(self.ledger.actor("a")["identity"], new)
        self.assertTrue(self.ledger.interrupt("a", old_active["turn_key"], old_active["epoch"]))
        self.ledger.start_turn("a", "new-epoch", "new-turn", native_identity=new)
        self.assertFalse(self.ledger.interrupt("a", old_active["turn_key"], old_active["epoch"]))
        self.assertEqual(self.ledger.active_turn("a")["identity"], new)
        self.assertEqual(self.ledger.active_turn("a")["turn_key"], "new-turn")

    def test_automatic_admission_freezes_supplied_owner_without_rebinding_on_duplicate(self):
        verified = {"session_id": "a", "target": {"pid": 41, "native_epoch": "verified"}}
        self.ingest(self.post("request"))
        self.ledger.create_attempt("a", "epoch", "wake")
        self.ledger.admit("wake", "a", "epoch", "turn", native_identity=verified)
        verified["target"]["pid"] = 99
        self.ledger.update_identity("a", verified)
        self.assertFalse(self.ledger.admit("wake", "a", "epoch", "turn", native_identity=verified))
        self.assertEqual(self.ledger.active_turn("a")["identity"]["target"]["pid"], 41)
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual(self.ledger.active_turn("a")["identity"]["target"]["pid"], 41)

    def test_stale_verified_target_cannot_schedule_even_without_native_epoch_change(self):
        old = {"session_id": "a", "target": {"native_epoch": None, "cwd": "/old-route"}}
        fresh = {"session_id": "a", "target": {"native_epoch": None, "cwd": "/new-route"}}
        self.ledger.update_identity("a", old)
        self.ingest(self.post("pending"))
        self.ledger.update_identity("a", fresh)
        self.assertIsNone(self.ledger.create_attempt("a", "same-routing-epoch", expected_identity=old))
        self.assertEqual(self.ledger.attempts("a"), [])
        self.assertEqual([p["id"] for p in self.ledger.unread_posts("a")], ["pending"])
        attempt = self.ledger.create_attempt("a", "same-routing-epoch", expected_identity=fresh)
        self.assertEqual(attempt["post_ids"], ["pending"])

    def test_prior_active_turn_owner_remains_unknown_instead_of_using_current_target(self):
        self.ledger.start_turn("a", "epoch", "turn")
        self.ledger.db.execute("ALTER TABLE native_turns DROP COLUMN identity_json")
        self.ledger.update_identity("a", {"session_id": "a", "target": {"pid": 999}})
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertIsNone(self.ledger.active_turn("a")["identity"])
        self.assertEqual(self.ledger.active_turn("a")["turn_key"], "turn")

    def test_unlimited_admissions_across_chats_interruptions_and_legacy_restart(self):
        self.chat("api")
        # Persist the exact old exhausted state; upgrading must ignore it, not reset it.
        self.ledger.db.execute("CREATE TABLE chains(id TEXT PRIMARY KEY, admissions INTEGER NOT NULL)")
        self.ledger.db.execute("INSERT INTO chains VALUES('exchange', 999)")
        for number in range(20):
            if number == 8:
                self.ledger.close()
                self.ledger = Ledger(self.path, clock=lambda: self.now)
            channel = "build" if number % 2 else "api"
            self.ingest(self.post(str(number), channel=channel, when=101 + number,
                                  causal={"chain_ids": ["exchange", "second-origin"]}))
            attempt = self.ledger.create_attempt("a", "epoch", "wake-" + str(number))
            self.assertIsNotNone(attempt)
            self.assertTrue(self.ledger.admit(attempt["token"], "a", "epoch", "turn-" + str(number)))
            self.assertTrue(self.ledger.interrupt("a", "turn-" + str(number)))
        self.assertEqual(len(self.ledger.attempts("a")), 20)
        self.assertEqual(self.ledger.db.execute("SELECT admissions FROM chains WHERE id='exchange'").fetchone()[0], 999)
        self.assertEqual(self.ledger.causal_context("a")["chain_ids"], ["exchange", "second-origin"])
        self.ingest(self.post("new-human-request", when=130, channel="api"))
        fresh = self.ledger.create_attempt("a", "epoch")
        self.assertEqual(fresh["post_ids"], ["new-human-request"])
        self.assertTrue(self.ledger.admit(fresh["token"], "a", "epoch", "human-turn"))

    def test_outbound_intent_survives_lost_response_and_retains_provenance(self):
        self.ingest(self.post("request", causal="chain"))
        self.ledger.create_attempt("a", "epoch", "wake")
        self.ledger.admit("wake", "a", "epoch", "turn")
        operation = self.ledger.prepare_outbound("a", {"channel_id": "other-room", "message": "reply"}, "op")
        self.assertEqual(operation["payload"]["causal"], {"chain_ids": ["chain"]})
        with self.assertRaisesRegex(LedgerError, "provenance"):
            self.ledger.prepare_outbound("a", {"message": "bypass", "causal": "different"})
        self.ledger.set_outbound("op", "uncertain", error="HTTP response lost")
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.outbounds()[0]["state"], "uncertain")
        with self.assertRaisesRegex(LedgerError, "reconciled"):
            self.ledger.set_outbound("op", "pending")
        with self.assertRaisesRegex(LedgerError, "different actor or payload"):
            self.ledger.prepare_outbound("a", {"message": "different"}, "op")
        self.ledger.set_outbound("op", "complete", {"post_id": "canonical-reply"})
        with self.assertRaisesRegex(LedgerError, "cannot change"):
            self.ledger.set_outbound("op", "complete", {"post_id": "duplicate"})
        self.assertEqual(self.ledger.outbound("op")["result"], {"post_id": "canonical-reply"})


    def test_schema_three_upgrade_preserves_frozen_notices_and_refuses_old_reader(self):
        self.ingest(self.post("A"))
        self.ledger.create_attempt("a", "epoch", "old-notice")
        self.ledger.set_transport("old-notice", "uncertain", {"queue_id": "original-row"})
        self.ledger.close()
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE attempts DROP COLUMN agent_seen_at")
            db.execute("CREATE UNIQUE INDEX one_open_attempt ON attempts(actor) WHERE status='pending'")
            db.execute("PRAGMA user_version=3")
            old_columns = [row[1] for row in db.execute("PRAGMA table_info(attempts)")]
            frozen = db.execute("SELECT * FROM attempts").fetchall()
            receipts = db.execute("SELECT * FROM receipts").fetchall()
            actors = db.execute("SELECT * FROM actors").fetchall()
        self.ledger = Ledger(self.path, clock=lambda: self.now)
        self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual([tuple(row) for row in self.ledger.db.execute(
            "SELECT " + ",".join(old_columns) + " FROM attempts")], frozen)
        self.assertEqual([tuple(row) for row in self.ledger.db.execute("SELECT * FROM receipts")], receipts)
        self.assertEqual([tuple(row) for row in self.ledger.db.execute("SELECT * FROM actors")], actors)
        self.assertIsNone(self.ledger.attempt("old-notice")["agent_seen_at"])
        self.assertIsNone(self.ledger.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='one_open_attempt'").fetchone())
        self.ingest(self.post("B", when=102))
        self.assertEqual(self.ledger.create_attempt("a", "epoch", "new-notice")["post_ids"], ["B"])
        with patch("murmur.ledger.LEDGER_SCHEMA_VERSION", 3), self.assertRaisesRegex(
                LedgerError, "schema 4 is newer than supported schema 3"):
            Ledger(self.path)
        self.assertEqual(len(self.ledger.pending_attempts("a")), 2)


if __name__ == "__main__":
    unittest.main()
