"""Local coordination state. Mattermost remains the message-body authority.

Every mutating method is one SQLite transaction, including feed cursor updates.
Instances can safely share a file across the coordinator, CLI and native hooks.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from .protocol import ATTENTION_VERSION


LEDGER_SCHEMA_VERSION = 4


class LedgerError(ValueError):
    """An operation would violate a persisted delivery or identity binding."""


class StaleFeedError(LedgerError):
    """A concurrent sync committed a newer access/feed snapshot."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _context_metadata(value, *, required=False):
    """Bound opaque native metadata without interpreting a provider's receipt format."""
    if value is None and not required:
        return None
    if not isinstance(value, dict) or (required and not value):
        raise LedgerError("Native context evidence must be an object" + (" with evidence" if required else ""))
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > 65536:
        raise LedgerError("Native context evidence exceeds 64 KiB")
    return encoded


def _owner(identity):
    return (identity.get("provider"), identity.get("session_id"), _json(identity.get("target", {})))


def _peer_metadata(value):
    """Bound the sealed peer envelope; native code validates its opaque file/frame proof."""
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise LedgerError("Claude peer evidence must be finite JSON") from error
    if not isinstance(value, dict) or not value or len(encoded.encode("utf-8")) > 65536:
        raise LedgerError("Claude peer evidence must be a bounded nonempty object")
    return encoded


def _peer_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _peer_turn(identity, turn_key):
    engine = identity.get("target", {}).get("engine_id")
    prefix = "claude:" + str(engine) + ":"
    if (not _peer_uuid(engine) or not isinstance(turn_key, str) or not turn_key.startswith(prefix)
            or not _peer_uuid(turn_key[len(prefix):])):
        raise LedgerError("Claude peer turn must name the frozen native engine and prompt")
    return turn_key[len(prefix):]


def _peer_identity(actor, epoch, identity):
    if not isinstance(identity, dict) or not isinstance(identity.get("target"), dict):
        raise LedgerError("Claude peer native identity is invalid")
    session, raw_epoch = identity.get("session_id"), identity["target"].get("native_epoch")
    if (identity.get("provider") != "claude" or not isinstance(session, str)
            or not session.startswith("local_") or not _peer_uuid(session[6:])
            or actor != "claude:" + session or not isinstance(raw_epoch, str) or not raw_epoch
            or len(raw_epoch) > 4096):
        raise LedgerError("Claude peer recipient or native owner generation is invalid")
    # This is the routing binding, whose JSON intentionally matches coordinator.routing_binding.
    binding = hashlib.sha256(json.dumps({"actor": actor, "native_epoch": raw_epoch},
                                       sort_keys=True).encode()).hexdigest()
    if epoch != binding:
        raise LedgerError("Claude peer routing epoch does not match its native owner")
    return raw_epoch


def _peer_request(actor, epoch, turn_key, identity, request):
    encoded = _peer_metadata(request)
    raw_epoch = _peer_identity(actor, epoch, identity)
    prompt = _peer_turn(identity, turn_key)
    if (type(request.get("schema")) is not int or request["schema"] != 1
            or request.get("kind") != "claude-peer-v1" or request.get("actor") != actor
            or request.get("session_id") != identity["session_id"]
            or request.get("engine_id") != identity["target"]["engine_id"]
            or request.get("epoch") != raw_epoch or request.get("turn_key") != turn_key
            or request.get("prompt_id") != prompt or not isinstance(request.get("profile"), dict)
            or not request["profile"]):
        raise LedgerError("Claude peer request recipient, owner, profile or turn does not match")
    return encoded


def _chains(causal, post_id=None):
    if isinstance(causal, str):
        values = [causal]
    elif isinstance(causal, dict):
        values = causal.get("chain_ids") or [causal.get("chain_id")]
    elif causal is None:
        values = []
    else:
        raise LedgerError("Invalid causal provenance")
    if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values if v is not None):
        raise LedgerError("Invalid causal chain IDs")
    result = sorted(set(v for v in values if v))
    return result or (["origin:" + post_id] if post_id else [])


class Ledger:
    def __init__(self, path, *, clock=None, timeout=5):
        self.path = str(path)
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > LEDGER_SCHEMA_VERSION:
            self.db.close()
            raise LedgerError("Ledger schema " + str(version) + " is newer than supported schema " +
                              str(LEDGER_SCHEMA_VERSION) + "; use a compatible Murmur runtime")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS actors (
                actor TEXT PRIMARY KEY, name TEXT NOT NULL, identity_json TEXT NOT NULL,
                user_id TEXT, enrolled_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                cursor INTEGER NOT NULL DEFAULT 0, generation INTEGER,
                causal_json TEXT NOT NULL DEFAULT '[]',
                mode TEXT NOT NULL DEFAULT 'native' CHECK(mode IN ('native','manual')),
                username_prefix TEXT, enrollment_id TEXT, enrollment_seq INTEGER
            );
            CREATE TABLE IF NOT EXISTS conversations (
                actor TEXT NOT NULL REFERENCES actors(actor), id TEXT NOT NULL,
                name TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL,
                muted INTEGER NOT NULL, mute_explicit INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(actor,id)
            );
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
                author TEXT NOT NULL, root_id TEXT NOT NULL,
                create_at INTEGER NOT NULL, update_at INTEGER NOT NULL,
                causal_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                arrival INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL REFERENCES actors(actor),
                post_id TEXT NOT NULL REFERENCES posts(id),
                read_at INTEGER, announced_at INTEGER, unavailable_at INTEGER,
                notification_eligible INTEGER NOT NULL DEFAULT 0, UNIQUE(actor,post_id)
            );
            CREATE INDEX IF NOT EXISTS receipt_actor ON receipts(actor,arrival);
            CREATE TABLE IF NOT EXISTS attempts (
                token TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES actors(actor),
                epoch TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                transport TEXT NOT NULL DEFAULT 'pending', evidence_json TEXT,
                post_ids_json TEXT NOT NULL, channels_json TEXT NOT NULL,
                causal_json TEXT NOT NULL, turn_key TEXT,
                created_at INTEGER NOT NULL, admitted_at INTEGER,
                completed_at INTEGER, reason TEXT,
                mode TEXT NOT NULL DEFAULT 'idle', expected_turn_key TEXT, tool_id TEXT,
                native_identity_json TEXT, receipt_request_json TEXT,
                emitted_at INTEGER, emission_json TEXT, verification_json TEXT,
                agent_seen_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS native_turns (
                actor TEXT PRIMARY KEY REFERENCES actors(actor),
                epoch TEXT NOT NULL, turn_key TEXT NOT NULL, token TEXT,
                identity_json TEXT
            );
            CREATE TABLE IF NOT EXISTS native_turn_records (
                actor TEXT NOT NULL REFERENCES actors(actor), epoch TEXT NOT NULL,
                turn_key TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(actor,epoch,turn_key)
            );
            CREATE TABLE IF NOT EXISTS outbound (
                op_id TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES actors(actor),
                payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT, error TEXT, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS onboarding (
                actor TEXT PRIMARY KEY REFERENCES actors(actor), choice TEXT NOT NULL,
                source_actor TEXT, snapshot_json TEXT, progress_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                plan_id TEXT
            );
        """)
        try:
            self._upgrade_schema(version)
        except BaseException:
            self.db.close()
            raise

    def _upgrade_schema(self, version):
        with self._write():
            actor_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(actors)")}
            if "mode" not in actor_columns:
                self.db.execute("ALTER TABLE actors ADD COLUMN mode TEXT NOT NULL DEFAULT 'native' "
                                "CHECK(mode IN ('native','manual'))")
            if "username_prefix" not in actor_columns:
                self.db.execute("ALTER TABLE actors ADD COLUMN username_prefix TEXT")
            for name, definition in (("enrollment_id", "TEXT"), ("enrollment_seq", "INTEGER")):
                if name not in actor_columns:
                    self.db.execute("ALTER TABLE actors ADD COLUMN " + name + " " + definition)
            # An old host's first-observation cutoff cannot be reconstructed.
            # Preserve existing eligibility and any unfetched pending arrivals.
            self.db.execute("UPDATE actors SET enrollment_seq=0 WHERE user_id IS NOT NULL AND enrollment_seq IS NULL")
            for row in self.db.execute("SELECT actor FROM actors WHERE enrollment_id IS NULL AND user_id IS NULL").fetchall():
                self.db.execute("UPDATE actors SET enrollment_id=? WHERE actor=?", (secrets.token_urlsafe(24), row[0]))
            if "plan_id" not in {row["name"] for row in self.db.execute("PRAGMA table_info(onboarding)")}:
                self.db.execute("ALTER TABLE onboarding ADD COLUMN plan_id TEXT")
            for row in self.db.execute("SELECT actor FROM onboarding WHERE plan_id IS NULL").fetchall():
                self.db.execute("UPDATE onboarding SET plan_id=? WHERE actor=?", (secrets.token_urlsafe(24), row[0]))
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(receipts)")}
            if "unavailable_at" not in columns:
                self.db.execute("ALTER TABLE receipts ADD COLUMN unavailable_at INTEGER")
            if "notification_eligible" not in columns:
                # Existing unannounced history has no proven arrival attention
                # state. Preserve unread mail, but never replay it as a wakeup.
                self.db.execute("ALTER TABLE receipts ADD COLUMN notification_eligible INTEGER NOT NULL DEFAULT 0")
            turn_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(native_turns)")}
            if "identity_json" not in turn_columns:
                # A mutable actor target cannot reconstruct a past owner's
                # process. Existing active turns retain unknown owner evidence.
                self.db.execute("ALTER TABLE native_turns ADD COLUMN identity_json TEXT")
            attempt_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(attempts)")}
            for name, definition in (
                ("mode", "TEXT NOT NULL DEFAULT 'idle'"), ("expected_turn_key", "TEXT"),
                ("tool_id", "TEXT"), ("native_identity_json", "TEXT"), ("receipt_request_json", "TEXT"),
                ("emitted_at", "INTEGER"), ("emission_json", "TEXT"), ("verification_json", "TEXT"),
                ("agent_seen_at", "INTEGER"),
            ):
                if name not in attempt_columns:
                    self.db.execute("ALTER TABLE attempts ADD COLUMN " + name + " " + definition)
            # A possibly sent batch reserves its own posts. Unconfirmed native
            # evidence must not prevent a later batch from being offered.
            self.db.execute("DROP INDEX IF EXISTS one_open_attempt")
            self.db.execute("CREATE INDEX IF NOT EXISTS attempt_actor ON attempts(actor)")
            for name, definition in (
                ("native_attempt_turn", "ON attempts(actor,epoch,turn_key) WHERE mode='idle' AND turn_key IS NOT NULL"),
                ("context_tool_once", "ON attempts(actor,epoch,expected_turn_key,tool_id) WHERE mode='context'"),
            ):
                sql = "CREATE UNIQUE INDEX " + name + " " + definition
                previous = self.db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
                if previous and previous["sql"] != sql:
                    self.db.execute("DROP INDEX " + name)
                if not previous or previous["sql"] != sql:
                    self.db.execute(sql)
            if version < 3:
                self._migrate_channel_names()
            self.db.execute("PRAGMA user_version=" + str(LEDGER_SCHEMA_VERSION))

    def _migrate_channel_names(self):
        """Rename local access and frozen plan fields; immutable intents stay exact."""
        self.db.execute("UPDATE conversations SET kind='channel' WHERE kind='group'")
        for row in self.db.execute("SELECT actor,choice,snapshot_json,progress_json FROM onboarding").fetchall():
            choice, snapshot_json, progress_json = row["choice"], row["snapshot_json"], row["progress_json"]
            if choice == "pending-and-groups":
                choice = "pending-and-channels"
            snapshot = json.loads(snapshot_json or "null")
            progress = json.loads(progress_json)
            if (snapshot is not None and not isinstance(snapshot, dict)) or not isinstance(progress, dict):
                raise LedgerError("Cannot migrate malformed frozen onboarding state")
            if snapshot is not None and "groups" in snapshot:
                if "channels" in snapshot and _json(snapshot["channels"]) != _json(snapshot["groups"]):
                    raise LedgerError("Conflicting groups/channels in frozen onboarding snapshot")
                snapshot["channels"] = snapshot.pop("groups")
                snapshot_json = _json(snapshot)
            changed = False
            for key, state in tuple(progress.items()):
                if key.startswith("group:"):
                    target = "channel:" + key[len("group:"):]
                    if target in progress and progress[target] != state:
                        raise LedgerError("Conflicting group/channel steps in frozen onboarding progress")
                    progress[target] = progress.pop(key)
                    changed = True
            if changed:
                progress_json = _json(progress)
            if (choice, snapshot_json, progress_json) != (row["choice"], row["snapshot_json"], row["progress_json"]):
                self.db.execute("UPDATE onboarding SET choice=?,snapshot_json=?,progress_json=? WHERE actor=?",
                                (choice, snapshot_json, progress_json, row["actor"]))

    def close(self):
        self.db.close()

    @contextmanager
    def _write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _require_actor(self, actor):
        row = self.db.execute("SELECT * FROM actors WHERE actor=?", (actor,)).fetchone()
        if row is None:
            raise LedgerError("Session is not explicitly enrolled")
        return row

    def _check_identity_refresh(self, previous, identity):
        if not isinstance(identity, dict):
            raise LedgerError("Native identity must be an object")
        for field in ("provider", "session_id"):
            if field in previous and previous[field] != identity.get(field):
                raise LedgerError("Enrolled native provider and session are immutable")
        if previous.get("parent_known") is True:
            if (identity.get("parent_known") is not True
                    or identity.get("parent_session_id") != previous.get("parent_session_id")):
                raise LedgerError("Verified native ancestry is immutable")

    def refresh_native_evidence(self, actor, expected_identity, identity):
        """Learn captured client facts without changing routing or frozen work.

        A mismatched snapshot is a harmless skip: normal Submit handling may
        independently establish a new route under its existing rules.
        """
        if {k: v for k, v in expected_identity.items() if k != "evidence"} != {
                k: v for k, v in identity.items() if k != "evidence"}:
            return False
        if not isinstance(identity.get("evidence"), dict):
            raise LedgerError("Captured native evidence must be an object")
        _context_metadata(identity["evidence"], required=False)
        if identity.get("evidence") == expected_identity.get("evidence"):
            return False
        with self._write():
            account = self._require_actor(actor)
            if account["mode"] == "manual" or account["identity_json"] != _json(expected_identity):
                return False
            self.db.execute("UPDATE actors SET identity_json=?,updated_at=? WHERE actor=?",
                            (_json(identity), self.clock(), actor))
        return True

    def enroll(self, actor, identity, name, user_id=None, *, mode="native", username_prefix=None, onboarding=None):
        """Persist explicit opt-in intent before server provisioning; retries retain its time."""
        if mode not in ("native", "manual"):
            raise LedgerError("Unsupported enrollment mode")
        now = self.clock()
        with self._write():
            old = self.db.execute("SELECT user_id,identity_json,mode FROM actors WHERE actor=?", (actor,)).fetchone()
            if old and old["mode"] != mode:
                raise LedgerError("Enrollment mode is immutable")
            if old and old["user_id"] and user_id and old["user_id"] != user_id:
                raise LedgerError("Enrolled account mapping cannot change")
            self._check_identity_refresh(json.loads(old["identity_json"]) if old else {}, identity)
            self.db.execute("""INSERT INTO actors(actor,name,identity_json,user_id,enrolled_at,updated_at,mode,username_prefix,enrollment_id,enrollment_seq)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(actor) DO UPDATE SET
                name=excluded.name,identity_json=excluded.identity_json,
                user_id=COALESCE(excluded.user_id,actors.user_id),updated_at=excluded.updated_at,
                username_prefix=COALESCE(excluded.username_prefix,actors.username_prefix)""",
                (actor, name, _json(identity), user_id, now, now, mode, username_prefix,
                 secrets.token_urlsafe(24), 0 if user_id else None))
            if onboarding is not None:
                choice, source = onboarding
                previous = self.onboarding(actor)
                if previous and (previous["choice"], previous["source_actor"]) != (choice, source):
                    # A failed capture can be abandoned before any import step
                    # has run. Once effects begin the choice is immutable.
                    if choice != "fresh" or previous["status"] != "pending" or previous["progress"]:
                        raise LedgerError("Fork onboarding choice is already recorded; resume it without changing --inherit")
                    self.db.execute("DELETE FROM onboarding WHERE actor=?", (actor,))
                    previous = None
                if not previous:
                    self.db.execute("""INSERT INTO onboarding(actor,choice,source_actor,status,created_at,updated_at,plan_id)
                        VALUES(?,?,?,?,?,?,?)""", (actor, choice, source,
                            "complete" if choice == "fresh" else "pending", now, now, secrets.token_urlsafe(24)))
        return self.actor(actor)

    def onboarding(self, actor):
        row = self.db.execute("SELECT * FROM onboarding WHERE actor=?", (actor,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json") or "null")
        result["progress"] = json.loads(result.pop("progress_json"))
        return result

    def require_onboarding(self, actor, plan_id):
        plan = self.onboarding(actor)
        if plan is None or plan["plan_id"] != plan_id:
            raise LedgerError("Onboarding plan changed; stale registration cannot continue")
        return plan

    def capture_pending(self, actor, plan_id):
        """Freeze local parent references/preferences together; never synchronize it."""
        with self._write():
            plan = self.require_onboarding(actor, plan_id)
            if plan["snapshot"] is not None:
                return plan["snapshot"]
            parent = self._require_actor(plan["source_actor"])
            if not parent["user_id"] or parent["mode"] != "native":
                raise LedgerError("Pending import requires the direct parent enrolled in this coordinator")
            unread = [dict(row) for row in self.db.execute("""SELECT p.id,p.channel_id,p.user_id,p.root_id
                FROM receipts r JOIN posts p ON p.id=r.post_id JOIN conversations c
                  ON c.actor=r.actor AND c.id=p.channel_id
                WHERE r.actor=? AND r.read_at IS NULL AND r.unavailable_at IS NULL
                  AND c.available=1 AND p.user_id<>? ORDER BY r.arrival""",
                (plan["source_actor"], parent["user_id"]))]
            channels = [dict(row) for row in self.db.execute("""SELECT id,muted FROM conversations
                WHERE actor=? AND available=1 AND kind='channel' AND role='member' ORDER BY id""",
                (plan["source_actor"],))] if plan["choice"] == "pending-and-channels" else []
            snapshot = {"unread": unread, "channels": channels, "local_captured_at": self.clock()}
            self.db.execute("UPDATE onboarding SET snapshot_json=?,updated_at=? WHERE actor=?",
                            (_json(snapshot), self.clock(), actor))
        return snapshot

    def capture_saved(self, actor, plan_id, post_ids, started_at, finished_at):
        with self._write():
            snapshot = self.require_onboarding(actor, plan_id)["snapshot"]
            if "saved_ids" not in snapshot:
                snapshot.update(saved_ids=sorted(set(post_ids)), saved_started_at=started_at,
                                saved_finished_at=finished_at)
                self.db.execute("UPDATE onboarding SET snapshot_json=?,updated_at=? WHERE actor=?",
                                (_json(snapshot), self.clock(), actor))
        return snapshot

    def onboarding_step(self, actor, plan_id, key, state, *, post=None, mute=None):
        """Receipt import/progress and preference/progress share local transactions."""
        with self._write():
            plan = self.require_onboarding(actor, plan_id)
            previous = plan["progress"].get(key)
            if state not in ("started", "done", "unavailable", "failed", "skipped"):
                raise LedgerError("Invalid onboarding step state")
            if state == "started" and previous not in (None, "failed"):
                return False
            if previous in ("done", "unavailable", "skipped"):
                return False
            if post is not None:
                account = self._require_actor(actor)
                if not self.db.execute("SELECT 1 FROM conversations WHERE actor=? AND id=? AND available=1",
                                       (actor, post["channel_id"])).fetchone():
                    raise LedgerError("Imported pending conversation is not accessible; retry registration")
                self._ingest(actor, account, [(post, False, 0)], account["cursor"], account["generation"])
                # A concurrent feed may already have inserted this receipt.
                # The imported reminder is quiet without rewriting a read or
                # a real native admission that has already occurred.
                self.db.execute("UPDATE receipts SET notification_eligible=0 WHERE actor=? AND post_id=?",
                                (actor, post["id"]))
            if mute is not None:
                channel = key.removeprefix("channel:")
                if not self.db.execute("SELECT 1 FROM conversations WHERE actor=? AND id=? AND available=1 AND role='member'",
                                       (actor, channel)).fetchone():
                    raise LedgerError("Imported channel membership changed; retry registration")
                self.db.execute("""UPDATE conversations SET muted=?,mute_explicit=1
                    WHERE actor=? AND id=? AND mute_explicit=0""", (bool(mute), actor, channel))
                self._suppress_unannounced(actor)
            plan["progress"][key] = state
            self.db.execute("UPDATE onboarding SET progress_json=?,updated_at=? WHERE actor=?",
                            (_json(plan["progress"]), self.clock(), actor))
        return True

    def finish_onboarding(self, actor, plan_id, error=None):
        with self._write():
            self.require_onboarding(actor, plan_id)
            self.db.execute("""UPDATE onboarding SET status=?,error=?,updated_at=? WHERE actor=?
                AND (status<>'complete' OR ? IS NULL)""",
                            ("pending" if error else "complete", error, self.clock(), actor, error))

    def skip_onboarding_save(self, actor, plan_id, key):
        """Resolve only a held save, without repeating any external effect."""
        with self._write():
            plan = self.require_onboarding(actor, plan_id)
            if not key.startswith("star:") or plan["progress"].get(key) != "started":
                raise LedgerError("Only a held saved-copy step can be skipped")
            plan["progress"][key] = "skipped"
            self.db.execute("UPDATE onboarding SET progress_json=?,error=NULL,updated_at=? WHERE actor=? AND plan_id=?",
                            (_json(plan["progress"]), self.clock(), actor, plan_id))

    def onboarding_ready(self, actor):
        return not self.db.execute("SELECT 1 FROM onboarding WHERE actor=? AND status<>'complete'", (actor,)).fetchone()

    def actor(self, actor):
        row = dict(self._require_actor(actor))
        row["identity"] = json.loads(row.pop("identity_json"))
        row["causal"] = {"chain_ids": json.loads(row.pop("causal_json"))}
        return row

    def actors(self):
        return [self.actor(row[0]) for row in self.db.execute("SELECT actor FROM actors ORDER BY actor")]

    def update_identity(self, actor, identity):
        """Store freshly verified target metadata; epoch-specific attempts are never rebound."""
        with self._write():
            old = self._require_actor(actor)
            self._check_identity_refresh(json.loads(old["identity_json"]), identity)
            self.db.execute("UPDATE actors SET identity_json=?,updated_at=? WHERE actor=?",
                            (_json(identity), self.clock(), actor))

    def set_user_id(self, actor, user_id):
        with self._write():
            row = self._require_actor(actor)
            if row["user_id"] and row["user_id"] != user_id:
                raise LedgerError("Enrolled account mapping cannot change")
            self.db.execute("UPDATE actors SET user_id=?,updated_at=? WHERE actor=?",
                            (user_id, self.clock(), actor))

    def set_registration(self, actor, user_id, enrollment_seq):
        if not isinstance(user_id, str) or not user_id or type(enrollment_seq) is not int or enrollment_seq < 0:
            raise LedgerError("Registration requires a canonical account and enrollment sequence")
        with self._write():
            row = self._require_actor(actor)
            if row["user_id"] and row["user_id"] != user_id:
                raise LedgerError("Enrolled account mapping cannot change")
            if row["enrollment_seq"] is not None and row["enrollment_seq"] != enrollment_seq:
                raise LedgerError("Canonical enrollment boundary cannot change")
            self.db.execute("UPDATE actors SET user_id=?,enrollment_seq=?,updated_at=? WHERE actor=?",
                            (user_id, enrollment_seq, self.clock(), actor))

    def sync_conversations(self, actor, snapshots):
        """Replace access metadata without resetting read state or explicit attention choices."""
        with self._write():
            self._require_actor(actor)
            self._sync_conversations(actor, snapshots)

    def _sync_conversations(self, actor, snapshots):
        self.db.execute("UPDATE conversations SET available=0 WHERE actor=?", (actor,))
        for chat in snapshots:
            if chat["role"] not in ("member", "observer") or chat["kind"] not in ("dm", "channel"):
                raise LedgerError("Invalid conversation role or kind")
            self.db.execute("""INSERT INTO conversations(actor,id,name,kind,role,muted)
                VALUES(?,?,?,?,?,?) ON CONFLICT(actor,id) DO UPDATE SET
                name=excluded.name,kind=excluded.kind,role=excluded.role,available=1,
                muted=CASE WHEN conversations.mute_explicit THEN conversations.muted ELSE excluded.muted END""",
                (actor, chat["id"], chat["name"], chat["kind"], chat["role"], chat["role"] == "observer"))
        self._suppress_unannounced(actor)

    def conversations(self, actor):
        self._require_actor(actor)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM conversations WHERE actor=? AND available=1 ORDER BY name,id", (actor,))]

    def set_mute(self, actor, channel_id, muted):
        with self._write():
            changed = self.db.execute("""UPDATE conversations SET muted=?,mute_explicit=1
                WHERE actor=? AND id=? AND available=1""", (bool(muted), actor, channel_id)).rowcount
            if not changed:
                raise LedgerError("Conversation is not accessible")
            if muted:
                self._suppress_unannounced(actor)

    def _suppress_unannounced(self, actor):
        """Attention/access revocation can suppress an arrival, never revive it."""
        self.db.execute("""UPDATE receipts SET notification_eligible=0
            WHERE actor=? AND announced_at IS NULL AND post_id IN (
                SELECT p.id FROM posts p JOIN conversations c ON c.id=p.channel_id
                WHERE c.actor=? AND (c.muted=1 OR c.available=0 OR c.role<>'member'))""", (actor, actor))

    def cursor(self, actor):
        row = self._require_actor(actor)
        return {"cursor": row["cursor"], "generation": row["generation"]}

    def ingest(self, actor, posts, cursor, generation):
        """In-process ingestion primitive; network pages must use apply_feed."""
        with self._write():
            current = self._require_actor(actor)
            if current["generation"] == generation and cursor < current["cursor"]:
                raise LedgerError("Feed cursor moved backwards without a generation reset")
            self._ingest(actor, current, ((post, True, cursor) for post in posts), cursor, generation)

    def _ingest(self, actor, current, posts, cursor, generation):
        notifying_channels = {row[0] for row in self.db.execute(
            "SELECT id FROM conversations WHERE actor=? AND available=1 AND muted=0 AND role='member'", (actor,))}
        for post, attention, sequence in posts:
            if post.get("system") or post.get("type", "").startswith("system_"):
                continue
            existing = self.db.execute("SELECT channel_id,user_id,create_at,root_id FROM posts WHERE id=?",
                                       (post["id"],)).fetchone()
            immutable = (post["channel_id"], post["user_id"], post["create_at"], post.get("root_id") or "")
            if existing and tuple(existing) != immutable:
                raise LedgerError("Canonical post identity changed")
            self.db.execute("""INSERT INTO posts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                author=CASE WHEN excluded.update_at>=posts.update_at THEN excluded.author ELSE posts.author END,
                update_at=MAX(posts.update_at,excluded.update_at)""",
                (post["id"], post["channel_id"], post["user_id"], post.get("author", post["user_id"]),
                 post.get("root_id") or "", post["create_at"], post.get("update_at", post["create_at"]),
                 _json(_chains(post.get("causal"), post["id"]))))
            if post["user_id"] != current["user_id"]:
                eligible = (attention and post["channel_id"] in notifying_channels
                            and current["enrollment_seq"] is not None and sequence > current["enrollment_seq"])
                self.db.execute("INSERT OR IGNORE INTO receipts(actor,post_id,notification_eligible) VALUES(?,?,?)",
                                (actor, post["id"], eligible))
        self.db.execute("UPDATE actors SET cursor=?,generation=? WHERE actor=?", (cursor, generation, actor))

    def apply_feed(self, actor, page):
        """Commit one fully validated access/attention page; reject stale writers."""
        if (not isinstance(page, dict) or type(page.get("attention_version")) is not int
                or page["attention_version"] != ATTENTION_VERSION):
            raise LedgerError("Murmur attention protocol " + str(ATTENTION_VERSION) +
                              " is required; update/restart the coordinator and plugin")
        for field in ("cursor", "generation"):
            if type(page.get(field)) is not int or page[field] < 0:
                raise LedgerError("Invalid feed " + field)
        if type(page.get("reset")) is not bool or type(page.get("more")) is not bool:
            raise LedgerError("Invalid feed reset/more flags")
        chats, entries = page.get("conversations"), page.get("changes")
        if not isinstance(chats, list) or not isinstance(entries, list) or len(entries) > 1000:
            raise LedgerError("A complete conversation snapshot and bounded change list are required")
        channels = set()
        for chat in chats:
            if (not isinstance(chat, dict) or not isinstance(chat.get("id"), str) or not chat["id"]
                    or chat["id"] in channels or not isinstance(chat.get("name"), str)
                    or chat.get("role") not in ("member", "observer") or chat.get("kind") not in ("dm", "channel")):
                raise LedgerError("Invalid or duplicate feed conversation")
            channels.add(chat["id"])
        sequence, policies, posts = 0, {}, []
        for entry in entries:
            if (not isinstance(entry, dict) or type(entry.get("notification_eligible")) is not bool
                    or type(entry.get("seq")) is not int or not sequence < entry["seq"] <= page["cursor"]
                    or type(entry.get("first_seen_seq")) is not int or not 0 < entry["first_seen_seq"] <= entry["seq"]):
                raise LedgerError("Invalid feed sequence or missing boolean attention hint")
            sequence = entry["seq"]
            post = entry.get("post")
            if (not isinstance(post, dict) or any(not isinstance(post.get(k), str) or not post[k]
                    for k in ("id", "channel_id", "user_id")) or post["channel_id"] not in channels
                    or not isinstance(post.get("root_id", ""), str)
                    or type(post.get("create_at")) is not int or post["create_at"] < 0
                    or type(post.get("update_at", post["create_at"])) is not int
                    or not isinstance(post.get("author", post["user_id"]), str)
                    or not isinstance(post.get("type", ""), str)):
                raise LedgerError("Invalid canonical post or unknown channel in feed snapshot")
            attention = entry["notification_eligible"]
            if post["id"] in policies and policies[post["id"]] != attention:
                raise LedgerError("Feed changes disagree on historical attention policy")
            policies[post["id"]] = attention
            _chains(post.get("causal"), post["id"])
            posts.append((post, attention, entry["first_seen_seq"]))
        with self._write():
            current = self._require_actor(actor)
            previous = current["generation"]
            if previous is not None:
                if page["generation"] < previous or (page["generation"] == previous
                                                     and page["cursor"] < current["cursor"]):
                    raise StaleFeedError("Feed snapshot moved backwards; retry from the current ledger state")
                if page["generation"] > previous and not page["reset"]:
                    raise LedgerError("Advancing feed generation requires an explicit reset")
            elif not page["reset"]:
                raise LedgerError("Initial feed snapshot requires an explicit reset")
            self._sync_conversations(actor, chats)
            self._ingest(actor, current, posts, page["cursor"], page["generation"])

    def _unread(self, actor, *, channel_id=None, root_id=None, post_id=None, eligible=False,
                attention=False, unoffered=False, order="chronological", limit=None):
        account = self._require_actor(actor)
        query = """SELECT p.*,r.arrival,r.read_at,r.announced_at,r.notification_eligible,c.name,c.kind,c.role,c.muted
            FROM receipts r JOIN posts p ON p.id=r.post_id
            JOIN conversations c ON c.actor=r.actor AND c.id=p.channel_id
            WHERE r.actor=? AND r.read_at IS NULL AND r.unavailable_at IS NULL AND c.available=1"""
        args = [actor]
        if account["user_id"]:
            query += " AND p.user_id<>?"
            args.append(account["user_id"])
        if channel_id is not None:
            query += " AND p.channel_id=?"
            args.append(channel_id)
        if root_id is not None:
            query += " AND (p.id=? OR p.root_id=?)"
            args.extend((root_id, root_id))
        if post_id is not None:
            query += " AND p.id=?"
            args.append(post_id)
        if eligible or attention:
            query += " AND r.notification_eligible=1 AND c.muted=0 AND c.role='member'"
        if eligible:
            query += " AND r.announced_at IS NULL"
        if unoffered:
            query += """ AND NOT EXISTS (
                SELECT 1 FROM attempts a,json_each(a.post_ids_json) reserved
                WHERE a.actor=r.actor AND a.transport<>'rejected' AND reserved.value=p.id)"""
        ordering = {"chronological": "p.create_at,p.id", "latest": "p.create_at DESC,p.id DESC",
                    "arrival": "r.arrival DESC"}
        query += " ORDER BY " + ordering[order]
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        rows = []
        for row in self.db.execute(query, args):
            item = dict(row)
            item["causal"] = {"chain_ids": json.loads(item.pop("causal_json"))}
            rows.append(item)
        return rows

    def notification_candidates(self, actor, *, limit=None):
        """New arrivals only; a possibly sent notice keeps its frozen cohort.

        Admission still uses base eligibility: reserving a batch is neither a
        read nor proof that the native client received it. Only a positive
        pre-write rejection releases its posts for another offer.
        """
        return self._unread(actor, eligible=True, unoffered=True, limit=limit)

    def unseen_notice_tokens(self, actor):
        """Snapshot live notification receipts, independently of native proof."""
        self._require_actor(actor)
        return [row[0] for row in self.db.execute("""SELECT token FROM attempts
            WHERE actor=? AND status IN ('pending','admitted') AND agent_seen_at IS NULL
            AND transport<>'rejected' ORDER BY created_at,token""", (actor,))]

    def acknowledge_notices(self, actor, tokens):
        """Record the agent's receipt without consuming posts or inventing admission.

        Validate the whole snapshot before updating any row. A concurrent new
        notification is not acknowledged unless its exact token was supplied.
        """
        if (not isinstance(tokens, (list, tuple))
                or any(not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token) is None
                       for token in tokens)):
            raise LedgerError("Notification receipt requires exact opaque tokens")
        with self._write():
            self._require_actor(actor)
            attempts = [self.attempt(token) for token in dict.fromkeys(tokens)]
            for attempt in attempts:
                if attempt["actor"] != actor:
                    raise LedgerError("Notification belongs to another recipient")
            now = self.clock()
            result = []
            for attempt in attempts:
                seen = attempt["agent_seen_at"]
                result.append({"token": attempt["token"], "seen_at": now if seen is None else seen,
                               "already_seen": seen is not None})
                if seen is None:
                    self.db.execute("UPDATE attempts SET agent_seen_at=? WHERE token=?", (now, attempt["token"]))
        return result

    def has_pending_attention(self, actor):
        """A bounded local reminder check; muted/reference mail stays quiet."""
        account = self.db.execute("SELECT mode,user_id FROM actors WHERE actor=?", (actor,)).fetchone()
        if (not account or account["mode"] != "native" or not account["user_id"]
                or not self.onboarding_ready(actor)):
            return False
        if self._unread(actor, attention=True, limit=1):
            return True
        return self.db.execute("""SELECT 1 FROM attempts a,json_each(a.post_ids_json) reserved
            JOIN receipts r ON r.actor=a.actor AND r.post_id=reserved.value
            JOIN posts p ON p.id=r.post_id
            JOIN conversations c ON c.actor=r.actor AND c.id=p.channel_id
            WHERE a.actor=? AND a.status='pending' AND a.agent_seen_at IS NULL
                AND a.transport<>'rejected' AND r.notification_eligible=1 AND r.unavailable_at IS NULL
                AND c.available=1 AND c.role='member' AND c.muted=0 LIMIT 1""", (actor,)).fetchone() is not None

    def unread_posts(self, actor, channel_id=None, root_id=None, limit=20):
        """Select exact unread IDs; caller fetches canonical bodies before mark_read."""
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise LedgerError("Read limit must be between 1 and 1000")
        return self._unread(actor, channel_id=channel_id, root_id=root_id, limit=limit)

    def mark_read(self, actor, post_ids):
        """Consume only IDs actually returned to this actor, never a time or arrival watermark."""
        with self._write():
            self._require_actor(actor)
            for post_id in set(post_ids):
                self.db.execute("UPDATE receipts SET read_at=COALESCE(read_at,?) WHERE actor=? AND post_id=?",
                                (self.clock(), actor, post_id))

    def mark_unavailable(self, actor, post_id):
        """Retire a canonical 404 for this actor without fabricating read/admission."""
        with self._write():
            self._require_actor(actor)
            self.db.execute("UPDATE receipts SET unavailable_at=COALESCE(unavailable_at,?) "
                            "WHERE actor=? AND post_id=?", (self.clock(), actor, post_id))

    def inbox(self, actor):
        """Unread conversation metadata, including muted mail; no state changes or body copies."""
        result = {}
        for post in self._unread(actor):
            chat = result.setdefault(post["channel_id"], {
                "id": post["channel_id"], "name": post["name"], "kind": post["kind"],
                "role": post["role"], "muted": bool(post["muted"]), "unread_count": 0})
            chat["unread_count"] += 1
            chat["latest_post_id"] = post["id"]
            chat["author"] = post["author"]
            chat["create_at"] = post["create_at"]
        return sorted(result.values(), key=lambda c: (-c["create_at"], c["id"]))

    def preview_candidates(self, actor, channel):
        """Quiet discovery, including announced work; no receipt mutation."""
        attention = self._unread(actor, channel_id=channel, attention=True, order="arrival", limit=20)
        return attention or self._unread(actor, channel_id=channel, order="latest", limit=1)

    def causal_context(self, actor):
        return self.actor(actor)["causal"]

    def resolve_causal(self, actor):
        """Explicitly resolve ambiguous continuation; an active automatic turn cannot shed provenance."""
        with self._write():
            self._require_actor(actor)
            if self.active_turn(actor):
                raise LedgerError("Cannot resolve provenance during an active native turn")
            self.db.execute("UPDATE actors SET causal_json='[]' WHERE actor=?", (actor,))

    def create_attempt(self, actor, epoch, token=None, *, expected_identity=None, refreshed_identity=None,
                       expected_enrollment=None):
        """Atomically refresh an idle route and freeze arrivals against its old snapshot."""
        if refreshed_identity is not None and expected_identity is None:
            raise LedgerError("Idle target refresh requires the pre-verification identity")
        with self._write():
            account = self._require_actor(actor)
            if account["mode"] == "manual" or not self.onboarding_ready(actor):
                return None
            if expected_identity is not None and account["identity_json"] != _json(expected_identity):
                return None
            if expected_enrollment is not None and any(
                    account[key] != expected_enrollment.get(key) for key in ("enrollment_id", "enrollment_seq", "user_id")):
                return None
            if self.active_turn(actor):
                return None
            posts = self.notification_candidates(actor)
            if not posts:
                return None
            snapshot, reason = None, None
            if refreshed_identity is not None:
                previous = json.loads(account["identity_json"])
                self._check_identity_refresh(previous, refreshed_identity)
                _context_metadata(refreshed_identity.get("evidence"), required=False)
                runtime_keys = {"desktop_wake", "codex_idle_thread"} if previous.get("provider") == "codex" else set()
                old_proof = {key: value for key, value in (previous.get("evidence") or {}).items() if key not in runtime_keys}
                new_proof = {key: value for key, value in (refreshed_identity.get("evidence") or {}).items() if key not in runtime_keys}
                if (not isinstance(refreshed_identity.get("target"), dict)
                        or old_proof != new_proof
                        or {key: value for key, value in previous.items() if key not in {"target", "evidence"}}
                        != {key: value for key, value in refreshed_identity.items() if key not in {"target", "evidence"}}):
                    raise LedgerError("Idle target refresh cannot change native identity, ancestry or non-runtime proof")
                snapshot = _json(refreshed_identity)
                if snapshot != account["identity_json"]:
                    self.db.execute("UPDATE actors SET identity_json=?,updated_at=? WHERE actor=?",
                                    (snapshot, self.clock(), actor))
                    reason = "Verified idle target refreshed"
            chains = set(json.loads(account["causal_json"]))
            for post in posts:
                chains.update(post["causal"]["chain_ids"])
            wake_token = token or secrets.token_urlsafe(24)
            channels = sorted({p["channel_id"] for p in posts})
            self.db.execute("""INSERT INTO attempts(token,actor,epoch,post_ids_json,channels_json,causal_json,
                created_at,native_identity_json,reason) VALUES(?,?,?,?,?,?,?,?,?)""",
                (wake_token, actor, epoch, _json([p["id"] for p in posts]), _json(channels),
                 _json(sorted(chains)), self.clock(), snapshot, reason))
        return self.attempt(wake_token)

    def claim_context(self, actor, epoch, turn_key, tool_id, *, native_identity, receipt_request=None, token=None):
        """Reserve one busy-hook handoff without replacing its enclosing native turn.

        Returning an attempt is a one-time claim, not native acceptance. A lost
        producer response remains uncertain, even when emitted_at is absent.
        """
        if not isinstance(tool_id, str) or not tool_id.strip() or len(tool_id) > 512:
            raise LedgerError("A bounded native tool ID is required")
        request = _context_metadata(receipt_request)
        with self._write():
            account = self._require_actor(actor)
            if account["mode"] == "manual" or not self.onboarding_ready(actor):
                return None
            self._check_identity_refresh(json.loads(account["identity_json"]), native_identity)
            active = self.active_turn(actor)
            if not active or (active["epoch"], active["turn_key"]) != (epoch, turn_key):
                return None
            if (not active["identity"] or _owner(active["identity"]) != _owner(native_identity)
                    or _owner(json.loads(account["identity_json"])) != _owner(native_identity)):
                return None
            if self.db.execute("""SELECT 1 FROM attempts WHERE mode='context' AND actor=? AND epoch=?
                    AND expected_turn_key=? AND tool_id=?""", (actor, epoch, turn_key, tool_id)).fetchone():
                return None
            if token and self.db.execute("SELECT 1 FROM attempts WHERE token=?", (token,)).fetchone():
                return None
            posts = self.notification_candidates(actor)
            if not posts:
                return None
            chains = set(json.loads(account["causal_json"]))
            for post in posts:
                chains.update(post["causal"]["chain_ids"])
            wake_token = token or secrets.token_urlsafe(24)
            causal = _json(sorted(chains))
            self.db.execute("""INSERT INTO attempts(token,actor,epoch,post_ids_json,channels_json,causal_json,
                turn_key,created_at,mode,expected_turn_key,tool_id,native_identity_json,receipt_request_json)
                VALUES(?,?,?,?,?,?,?,?,'context',?,?,?,?)""",
                (wake_token, actor, epoch, _json([p["id"] for p in posts]), _json(sorted({p["channel_id"] for p in posts})),
                 causal, turn_key, self.clock(), turn_key, tool_id, _json(native_identity), request))
            # A tool can send a reply immediately after the handoff, before a
            # later native-history verifier confirms that input's acceptance.
            self.db.execute("UPDATE actors SET causal_json=? WHERE actor=?", (causal, actor))
        return self.attempt(wake_token)

    def claim_peer(self, actor, epoch, turn_key, *, native_identity, receipt_request,
                   expected_identity, token=None):
        """Freeze a busy peer handoff without treating a socket write as admission."""
        request = _peer_request(actor, epoch, turn_key, native_identity, receipt_request)
        with self._write():
            account = self._require_actor(actor)
            if (account["mode"] != "native" or not account["user_id"] or not self.onboarding_ready(actor)
                    or account["identity_json"] != _json(expected_identity)):
                return None
            enrolled = json.loads(account["identity_json"])
            self._check_identity_refresh(enrolled, native_identity)
            active = self.active_turn(actor)
            if (not active or (active["epoch"], active["turn_key"]) != (epoch, turn_key)
                    or not active["identity"] or _owner(active["identity"]) != _owner(native_identity)
                    or _owner(enrolled) != _owner(native_identity)):
                return None
            if token and self.db.execute("SELECT 1 FROM attempts WHERE token=?", (token,)).fetchone():
                return None
            posts = self.notification_candidates(actor)
            if not posts:
                return None
            chains = set(json.loads(account["causal_json"]))
            for post in posts:
                chains.update(post["causal"]["chain_ids"])
            wake_token = token or secrets.token_urlsafe(24)
            if not isinstance(wake_token, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", wake_token) is None:
                raise LedgerError("Invalid opaque Claude peer token")
            causal = _json(sorted(chains))
            self.db.execute("""INSERT INTO attempts(token,actor,epoch,post_ids_json,channels_json,causal_json,
                turn_key,created_at,mode,expected_turn_key,tool_id,native_identity_json,receipt_request_json)
                VALUES(?,?,?,?,?,?,?,?,'context',?,NULL,?,?)""",
                (wake_token, actor, epoch, _json([p["id"] for p in posts]),
                 _json(sorted({p["channel_id"] for p in posts})), causal, turn_key, self.clock(), turn_key,
                 _json(native_identity), request))
            self.db.execute("UPDATE actors SET causal_json=? WHERE actor=?", (causal, actor))
        return self.attempt(wake_token)

    def _peer_binding(self, token, binding=None):
        attempt = self.attempt(token)
        request = attempt["receipt_request"]
        if (attempt["mode"] != "context" or attempt["tool_id"] is not None
                or not isinstance(request, dict) or request.get("kind") != "claude-peer-v1"):
            raise LedgerError("This notification does not use Claude peer delivery")
        expected = (attempt["actor"], attempt["epoch"], attempt["expected_turn_key"])
        if binding is not None and expected != binding:
            raise LedgerError("Claude peer recipient, owner or expected turn does not match")
        _peer_request(*expected, attempt["native_identity"], request)
        return attempt

    @staticmethod
    def _peer_emission(attempt, emission):
        encoded = _peer_metadata(emission)
        request_hash = hashlib.sha256(_json(attempt["receipt_request"]).encode()).hexdigest()
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "murmur:" + attempt["token"]))
        source, pid, frame_hash = emission.get("from"), emission.get("producer_pid"), emission.get("frame_sha256")
        if (type(emission.get("schema")) is not int or emission["schema"] != 1
                or emission.get("kind") != "claude-peer-v1" or emission.get("actor") != attempt["actor"]
                or emission.get("request_sha256") != request_hash or emission.get("message_id") != message_id
                or not isinstance(source, str) or not source.startswith("uds:/") or len(source) <= 5
                or type(pid) is not int or not 1 < pid < 2**31 or not isinstance(frame_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", frame_hash) is None):
            raise LedgerError("Claude peer sealed emission does not match its request and token")
        from .native.hooks import validate_doorbell
        try:
            validate_doorbell(emission.get("text"), attempt["token"])
        except ValueError as error:
            raise LedgerError("Claude peer emission does not carry its exact doorbell") from error
        return encoded

    def mark_peer_emitted(self, token, actor, epoch, expected_turn_key, emission):
        """Seal one deterministic frame before the native transport can write bytes."""
        with self._write():
            attempt = self._peer_binding(token, (actor, epoch, expected_turn_key))
            encoded = self._peer_emission(attempt, emission)
            if attempt["emitted_at"] is not None:
                if attempt["emission"] != emission:
                    raise LedgerError("Claude peer emission is immutable")
                return False
            if attempt["status"] != "pending" or attempt["transport"] != "pending":
                raise LedgerError("Claude peer attempt cannot emit after transport or retirement")
            self.db.execute("UPDATE attempts SET emitted_at=?,emission_json=? WHERE token=?",
                            (self.clock(), encoded, token))
        return True

    def set_peer_transport(self, token, status, evidence=None):
        """Hold possible writes; only a positive pre-write rejection releases eligibility."""
        if status not in ("accepted", "rejected", "uncertain"):
            raise LedgerError("Invalid Claude peer transport status")
        encoded = _context_metadata(evidence)
        with self._write():
            attempt = self._peer_binding(token)
            if status != "rejected":
                if attempt["emitted_at"] is None:
                    raise LedgerError("Claude peer transport requires a sealed emission")
                self._peer_emission(attempt, attempt["emission"])
            if status == "rejected" and attempt["verification"] is not None:
                return False  # Native admission outranks a late transport result.
            if status == "rejected" and attempt["transport"] in ("accepted", "uncertain"):
                raise LedgerError("A possible Claude peer write cannot become a pre-write rejection")
            if attempt["transport"] == "rejected" and status != "rejected":
                raise LedgerError("A rejected Claude peer attempt cannot be reused")
            if attempt["transport"] == status:
                return False
            self.db.execute("UPDATE attempts SET transport=?,evidence_json=? WHERE token=?", (status, encoded, token))
            if status == "rejected" and attempt["status"] == "pending":
                self.db.execute("UPDATE attempts SET status='retired',reason='peer transport rejected before write' WHERE token=?",
                                (token,))
        return True

    def record_peer_submit(self, token, actor, epoch, actual_turn_key, *, native_identity):
        """Observe an authentic peer Submit; only later native receipt proof announces posts."""
        with self._write():
            attempt = self._peer_binding(token)
            if (attempt["actor"], attempt["epoch"]) != (actor, epoch):
                raise LedgerError("Claude peer Submit recipient or routing owner changed")
            _peer_identity(actor, epoch, native_identity)
            _peer_turn(native_identity, actual_turn_key)
            if (attempt["emitted_at"] is None or attempt["transport"] == "rejected"
                    or attempt["status"] == "retired"
                    or _owner(attempt["native_identity"]) != _owner(native_identity)):
                raise LedgerError("Claude peer Submit has no sealed handoff for this native owner")
            self._peer_emission(attempt, attempt["emission"])
            if attempt["verification"] is not None and attempt["turn_key"] != actual_turn_key:
                raise LedgerError("Claude peer token already belongs to another actual turn")
            account = self._require_actor(actor)
            if (account["mode"] != "native"
                    or _owner(json.loads(account["identity_json"])) != _owner(native_identity)):
                raise LedgerError("Claude peer Submit enrolled owner changed")
            return self._start_turn(actor, epoch, actual_turn_key, supersede=True, native_identity=native_identity)

    def confirm_peer(self, token, actor, epoch, expected_turn_key, *, verification):
        """Bind a corroborated packet to its actual recorded turn, even after that turn ended."""
        encoded = _peer_metadata(verification)
        with self._write():
            attempt = self._peer_binding(token, (actor, epoch, expected_turn_key))
            if attempt["emitted_at"] is None or attempt["transport"] == "rejected":
                raise LedgerError("Claude peer admission requires a sealed possible handoff")
            self._peer_emission(attempt, attempt["emission"])
            actual = verification.get("actual_turn_key")
            _peer_turn(attempt["native_identity"], actual)
            if (verification.get("source") != "claude-peer-v1" or verification.get("actor") != actor
                    or verification.get("token") != token or verification.get("epoch") != attempt["receipt_request"]["epoch"]
                    or verification.get("expected_turn_key") != expected_turn_key or verification.get("turn_key") != actual
                    or verification.get("message_id") != attempt["emission"]["message_id"]
                    or verification.get("profile") != attempt["receipt_request"]["profile"]
                    or verification.get("delivery") not in ("queued_command", "peer_prompt")
                    or not _peer_uuid(verification.get("receipt_uuid"))
                    or type(verification.get("byte_start")) is not int or verification["byte_start"] < 0):
                raise LedgerError("Claude peer receipt recipient, owner, actual turn or seal does not match")
            if attempt["verification"] is not None:
                if attempt["turn_key"] != actual:
                    raise LedgerError("Claude peer actual admission is immutable")
                return False
            recorded = self.db.execute("SELECT status FROM native_turn_records WHERE actor=? AND epoch=? AND turn_key=?",
                                       (actor, epoch, actual)).fetchone()
            if not recorded or recorded["status"] not in ("active", "completed", "interrupted"):
                raise LedgerError("Claude peer actual native turn has not been recorded")
            active = self.active_turn(actor)
            if recorded["status"] == "active":
                if (not active or (active["epoch"], active["turn_key"]) != (epoch, actual)
                        or not active["identity"] or _owner(active["identity"]) != _owner(attempt["native_identity"])):
                    raise LedgerError("Claude peer actual native turn owner is unresolved")
                status, completed_at = "admitted", None
            else:
                status, completed_at = recorded["status"], self.clock()
            now = self.clock()
            for post_id in attempt["post_ids"]:
                self.db.execute("UPDATE receipts SET announced_at=COALESCE(announced_at,?) WHERE actor=? AND post_id=?",
                                (now, actor, post_id))
            self.db.execute("""UPDATE attempts SET turn_key=?,verification_json=?,status=?,admitted_at=?,completed_at=?
                WHERE token=?""", (actual, encoded, status, now, completed_at, token))
        return True

    def _context_binding(self, token, actor, epoch, turn_key, tool_id):
        attempt = self.attempt(token)
        if (attempt["mode"] != "context" or not isinstance(tool_id, str) or not tool_id.strip()
                or (isinstance(attempt["receipt_request"], dict)
                    and attempt["receipt_request"].get("kind") == "claude-peer-v1")):
            raise LedgerError("This notification does not use native context delivery")
        if (attempt["actor"], attempt["epoch"], attempt["expected_turn_key"], attempt["tool_id"]) != (actor, epoch, turn_key, tool_id):
            raise LedgerError("Native context recipient, owner, turn or tool does not match")
        return attempt

    def mark_context_emitted(self, token, actor, epoch, turn_key, tool_id, evidence=None):
        """Record possible handoff BEFORE stdout; never announce or permit replay."""
        emission = _context_metadata(evidence)
        with self._write():
            attempt = self._context_binding(token, actor, epoch, turn_key, tool_id)
            if attempt["status"] != "pending" or attempt["emitted_at"] is not None:
                return False
            self.db.execute("UPDATE attempts SET emitted_at=?,emission_json=? WHERE token=?",
                            (self.clock(), emission, token))
        return True

    def confirm_context(self, token, actor, epoch, turn_key, tool_id, *, verification):
        """Settle a trusted verifier's exact receipt, including after Stop or retirement.

        The verifier owns native-format checks. Later reads/mutes cannot undo an
        already handed-off input; confirmation never recreates a native turn.
        """
        evidence = _context_metadata(verification, required=True)
        with self._write():
            attempt = self._context_binding(token, actor, epoch, turn_key, tool_id)
            if attempt["verification"] is not None:
                return False
            active = self.active_turn(actor)
            if active and (active["epoch"], active["turn_key"]) == (epoch, turn_key):
                status, completed_at = "admitted", None
            else:
                finished = self.db.execute("SELECT status FROM native_turn_records WHERE actor=? AND epoch=? AND turn_key=?",
                                           (actor, epoch, turn_key)).fetchone()
                if not finished or finished["status"] not in ("completed", "interrupted"):
                    raise LedgerError("Native context enclosing turn state is unresolved")
                status, completed_at = finished["status"], self.clock()
            now = self.clock()
            for post_id in attempt["post_ids"]:
                self.db.execute("UPDATE receipts SET announced_at=COALESCE(announced_at,?) WHERE actor=? AND post_id=?",
                                (now, actor, post_id))
            self.db.execute("""UPDATE attempts SET verification_json=?,status=?,admitted_at=?,completed_at=?
                WHERE token=?""", (evidence, status, now, completed_at, token))
        return True

    def attempt(self, token):
        row = self.db.execute("SELECT * FROM attempts WHERE token=?", (token,)).fetchone()
        if not row:
            raise LedgerError("Unknown notification token")
        item = dict(row)
        item.pop("budget_state", None)  # Inert field retained by older ledgers.
        for field in ("post_ids", "channels"):
            item[field] = json.loads(item.pop(field + "_json"))
        item["causal"] = {"chain_ids": json.loads(item.pop("causal_json"))}
        item["evidence"] = json.loads(item.pop("evidence_json") or "null")
        for field in ("native_identity", "receipt_request", "emission", "verification"):
            item[field] = json.loads(item.pop(field + "_json") or "null")
        return item

    def attempts(self, actor=None):
        query, args = "SELECT token FROM attempts", ()
        if actor is not None:
            query, args = query + " WHERE actor=?", (actor,)
        return [self.attempt(r[0]) for r in self.db.execute(query + " ORDER BY created_at,token", args)]

    def pending_attempts(self, actor=None):
        query = "SELECT token FROM attempts WHERE status IN ('pending','admitted')"
        args = ()
        if actor is not None:
            query += " AND actor=?"
            args = (actor,)
        return [self.attempt(row[0]) for row in self.db.execute(query + " ORDER BY created_at,token", args)]

    def set_transport(self, token, status, evidence=None):
        """Record queue/socket outcome only. Acceptance never announces a post."""
        if status not in ("accepted", "rejected", "uncertain"):
            raise LedgerError("Invalid transport status")
        with self._write():
            attempt = self.attempt(token)
            if attempt["mode"] == "context":
                raise LedgerError("Use the native context handoff and receipt methods")
            self.db.execute("UPDATE attempts SET transport=?,evidence_json=? WHERE token=?",
                            (status, _json(evidence), token))
            if status == "rejected" and attempt["status"] == "pending":
                self.db.execute("UPDATE attempts SET status='retired',reason='transport rejected' WHERE token=?", (token,))

    def _idle_enrollment_matches(self, emission, account):
        gate = (emission or {}).get("gate")
        captured = gate.get("ledger") if isinstance(gate, dict) else None
        return isinstance(captured, dict) and all(key in captured and captured[key] == account[key]
                                                for key in ("enrollment_id", "enrollment_seq", "user_id"))

    def seal_idle_handoff(self, token, actor, epoch, emission, *, expected_enrollment):
        """Freeze optional restart evidence before the first possible enqueue."""
        encoded = _context_metadata(emission, required=True)
        if (type(emission.get("schema")) is not int or emission["schema"] != 1
                or emission.get("kind") != "codex-idle-handoff-v1"
                or emission.get("token") != token or emission.get("actor") != actor):
            raise LedgerError("Idle handoff proof does not match its recipient and token")
        from .native.hooks import validate_doorbell
        validate_doorbell(emission.get("text"), token)
        with self._write():
            attempt = self.attempt(token)
            account = self._require_actor(actor)
            frozen = attempt["native_identity"] or {}
            if (frozen.get("provider") != "codex"
                    or emission.get("session_id") != frozen.get("session_id")):
                raise LedgerError("Idle handoff proof differs from its frozen Codex session")
            if (attempt["actor"] != actor or attempt["epoch"] != epoch or attempt["mode"] != "idle"
                    or attempt["status"] != "pending" or attempt["transport"] != "pending"
                    or attempt["emission"] is not None or account["mode"] != "native"
                    or account["identity_json"] != _json(frozen)
                    or any(account[key] != expected_enrollment.get(key)
                           for key in ("enrollment_id", "enrollment_seq", "user_id"))
                    or not self._idle_enrollment_matches(emission, account)
                    or self.active_turn(actor)):
                return False
            if not any(post["id"] in attempt["post_ids"] for post in self._unread(actor, eligible=True)):
                return False
            self.db.execute("UPDATE attempts SET emission_json=? WHERE token=?", (encoded, token))
        return True

    def idle_wake_current(self, token, action_id, expected_identity):
        """Final local snapshot after native reprobe, immediately before wake."""
        with self._write():
            attempt = self.attempt(token)
            account = self._require_actor(attempt["actor"])
            recovery = (attempt["evidence"] or {}).get("recovery", {})
            return (attempt["mode"] == "idle" and attempt["status"] == "pending"
                    and attempt["agent_seen_at"] is None
                    and account["mode"] == "native" and account["identity_json"] == _json(expected_identity)
                    and self._idle_enrollment_matches(attempt["emission"], account)
                    and recovery.get("action_id") == action_id and recovery.get("lease_until", 0) > self.clock()
                    and not self.active_turn(attempt["actor"]) and self.onboarding_ready(attempt["actor"])
                    and any(post["id"] in attempt["post_ids"]
                            for post in self._unread(attempt["actor"], eligible=True)))

    def claim_idle_wake(self, token, expected_attempt, expected_identity, recovery, *, lease_ms=10000):
        """Lease a wake-only action, never another enqueue or an admission."""
        _context_metadata(recovery, required=True)
        payload_hash = recovery.get("queue_payload_sha256")
        if (not isinstance(recovery.get("queue_id"), str) or not 0 < len(recovery["queue_id"]) <= 128
                or not isinstance(recovery.get("generation"), str) or not 0 < len(recovery["generation"]) <= 128
                or not isinstance(payload_hash, str) or len(payload_hash) != 64
                or any(character not in "0123456789abcdef" for character in payload_hash)
                or type(lease_ms) is not int or not 1000 <= lease_ms <= 60000):
            raise LedgerError("Idle wake recovery requires a bounded queue/generation lease")
        with self._write():
            attempt = self.attempt(token)
            account = self._require_actor(attempt["actor"])
            if (attempt != expected_attempt or attempt["mode"] != "idle" or attempt["status"] != "pending"
                    or attempt["agent_seen_at"] is not None
                    or account["mode"] != "native" or account["identity_json"] != _json(expected_identity)
                    or self.active_turn(attempt["actor"]) or not self.onboarding_ready(attempt["actor"])):
                return None
            if (attempt["emission"] or {}).get("kind") != "codex-idle-handoff-v1":
                raise LedgerError("Legacy idle handoff has no frozen restart qualification")
            if not self._idle_enrollment_matches(attempt["emission"], account):
                return None
            if not any(post["id"] in attempt["post_ids"] for post in self._unread(attempt["actor"], eligible=True)):
                return None
            evidence = dict(attempt["evidence"] or {})
            previous = evidence.get("recovery", {})
            now = self.clock()
            if previous.get("lease_until", 0) > now:
                return None
            if previous.get("queue_id") not in (None, recovery["queue_id"]):
                raise LedgerError("Idle wake cannot switch its retained queue entry")
            if previous.get("queue_payload_sha256") not in (None, payload_hash):
                raise LedgerError("Idle wake cannot change its retained native payload")
            claim = {**recovery, "schema": 1, "kind": "codex-idle-wake-v1",
                     "action_id": secrets.token_urlsafe(18), "claimed_at": now,
                     "lease_until": now + lease_ms, "status": "dispatching"}
            evidence["recovery"] = claim
            encoded = _context_metadata(evidence, required=True)
            self.db.execute("UPDATE attempts SET evidence_json=? WHERE token=?", (encoded, token))
        return claim

    def finish_idle_wake(self, token, action_id, result):
        """A late wake result cannot overwrite actual native admission."""
        if result.get("status") not in ("accepted", "uncertain", "rejected"):
            raise LedgerError("Invalid wake-only transport result")
        with self._write():
            attempt = self.attempt(token)
            evidence = dict(attempt["evidence"] or {})
            recovery = evidence.get("recovery", {})
            if (attempt["mode"] != "idle" or attempt["status"] != "pending"
                    or recovery.get("action_id") != action_id):
                return False
            recovery = {**recovery, "status": result["status"], "finished_at": self.clock(),
                        "detail": str(result.get("detail", ""))[:1000]}
            evidence["recovery"] = recovery
            self.db.execute("UPDATE attempts SET evidence_json=? WHERE token=?",
                            (_context_metadata(evidence, required=True), token))
        return True

    def retire_attempt(self, token, reason):
        with self._write():
            attempt = self.attempt(token)
            if attempt["status"] != "pending":
                raise LedgerError("Only a pending notification can be retired")
            if attempt["mode"] == "context":
                # Missing output/receipt is not proof of non-consumption.
                # Suppress re-emission of the same possibly delivered arrivals.
                for post_id in attempt["post_ids"]:
                    self.db.execute("UPDATE receipts SET notification_eligible=0 WHERE actor=? AND post_id=?",
                                    (attempt["actor"], post_id))
            self.db.execute("UPDATE attempts SET status='retired',reason=? WHERE token=?", (reason, token))

    def active_turn(self, actor):
        row = self.db.execute("SELECT * FROM native_turns WHERE actor=?", (actor,)).fetchone()
        if row is None:
            return None
        active = dict(row)
        active["identity"] = json.loads(active.pop("identity_json") or "null")
        return active

    def _finish_active(self, active, status):
        self.db.execute("DELETE FROM native_turns WHERE actor=?", (active["actor"],))
        self.db.execute("UPDATE native_turn_records SET status=? WHERE actor=? AND epoch=? AND turn_key=?",
                        (status, active["actor"], active["epoch"], active["turn_key"]))
        self.db.execute("""UPDATE attempts SET status=?,completed_at=? WHERE actor=? AND epoch=? AND turn_key=?
            AND status='admitted'""", (status, self.clock(), active["actor"], active["epoch"], active["turn_key"]))

    def start_turn(self, actor, epoch, turn_key, *, supersede=False, native_identity=None):
        """Track a verified ordinary turn, freezing its owner independently of actor updates."""
        with self._write():
            return self._start_turn(actor, epoch, turn_key, supersede=supersede, native_identity=native_identity)

    def _start_turn(self, actor, epoch, turn_key, *, supersede=False, native_identity=None):
        """Record a turn while the caller holds the write transaction."""
        if not turn_key:
            raise LedgerError("Native turn key is required")
        account = self._require_actor(actor)
        if native_identity is not None:
            self._check_identity_refresh(json.loads(account["identity_json"]), native_identity)
        active = self.active_turn(actor)
        if active:
            if (active["epoch"], active["turn_key"]) == (epoch, turn_key):
                return False
        if self.db.execute("SELECT 1 FROM native_turn_records WHERE actor=? AND epoch=? AND turn_key=?",
                           (actor, epoch, turn_key)).fetchone():
            return False
        if active:
            if not supersede or active["epoch"] != epoch:
                raise LedgerError("A different native turn or epoch is already active")
            self._finish_active(active, "interrupted")
        snapshot = account["identity_json"] if native_identity is None else _json(native_identity)
        if native_identity is not None:
            self.db.execute("UPDATE actors SET identity_json=?,updated_at=? WHERE actor=?",
                            (snapshot, self.clock(), actor))
        self.db.execute("INSERT INTO native_turns(actor,epoch,turn_key,token,identity_json) VALUES(?,?,?,NULL,?)",
                        (actor, epoch, turn_key, snapshot))
        self.db.execute("INSERT INTO native_turn_records VALUES(?,?,?,'active')", (actor, epoch, turn_key))
        return True

    def admit(self, token, actor, epoch, turn_key, *, supersede=False, native_identity=None):
        """Bind a doorbell to actual native admission; exact duplicate returns False.

        Freshly muted/read/unavailable frozen posts cannot cause admission. The
        announcement covers the remaining frozen IDs, never concurrent arrivals.
        """
        if not turn_key:
            raise LedgerError("Native turn key is required")
        rejection = None
        with self._write():
            attempt = self.attempt(token)
            if attempt["mode"] != "idle":
                raise LedgerError("Native context requires a corroborated receipt, not Submit admission")
            if (attempt["actor"], attempt["epoch"]) != (actor, epoch):
                raise LedgerError("Notification recipient or native epoch does not match")
            account = self._require_actor(actor)
            if native_identity is not None:
                self._check_identity_refresh(json.loads(account["identity_json"]), native_identity)
            if attempt["status"] in ("admitted", "completed", "interrupted") and attempt["turn_key"] == turn_key:
                return False
            if attempt["status"] != "pending":
                raise LedgerError("Notification token is retired or already used")
            if self.db.execute("SELECT 1 FROM native_turn_records WHERE actor=? AND epoch=? AND turn_key=?",
                               (actor, epoch, turn_key)).fetchone():
                raise LedgerError("Native turn key has already admitted another notification")
            eligible = {p["id"] for p in self._unread(actor, eligible=True)}
            frozen = [p for p in attempt["post_ids"] if p in eligible]
            # An older queued notice may arrive after a newer one. Its frozen
            # provenance cannot erase unresolved work already in this session.
            chains = sorted(set(json.loads(account["causal_json"])) | set(attempt["causal"]["chain_ids"]))
            if not frozen:
                rejection = "No eligible frozen arrivals"
                self.db.execute("UPDATE attempts SET status='retired',reason=? WHERE token=?", (rejection, token))
            else:
                active = self.active_turn(actor)
                if active:
                    if not supersede or active["epoch"] != epoch:
                        raise LedgerError("A native turn or a different native epoch is already active")
                    self._finish_active(active, "interrupted")
                now = self.clock()
                for post_id in frozen:
                    self.db.execute("""UPDATE receipts SET announced_at=COALESCE(announced_at,?)
                        WHERE actor=? AND post_id=?""", (now, actor, post_id))
                self.db.execute("UPDATE actors SET causal_json=? WHERE actor=?", (_json(chains), actor))
                self.db.execute("""UPDATE attempts SET status='admitted',turn_key=?,admitted_at=?
                    WHERE token=?""", (turn_key, now, token))
                snapshot = account["identity_json"] if native_identity is None else _json(native_identity)
                if native_identity is not None:
                    self.db.execute("UPDATE actors SET identity_json=?,updated_at=? WHERE actor=?",
                                    (snapshot, now, actor))
                self.db.execute("INSERT INTO native_turns(actor,epoch,turn_key,token,identity_json) VALUES(?,?,?,?,?)",
                                (actor, epoch, turn_key, token, snapshot))
                self.db.execute("INSERT INTO native_turn_records VALUES(?,?,?,'active')", (actor, epoch, turn_key))
        if rejection:
            raise LedgerError(rejection)
        return True

    def _end_turn(self, actor, turn_key, status, epoch=None):
        with self._write():
            active = self.active_turn(actor)
            if not active or active["turn_key"] != turn_key or (epoch is not None and active["epoch"] != epoch):
                return False
            self._finish_active(active, status)
        return True

    def complete(self, actor, turn_key, epoch=None):
        """Release only the exact active turn. Its unresolved causal context persists."""
        return self._end_turn(actor, turn_key, "completed", epoch)

    def interrupt(self, actor, turn_key, epoch=None):
        return self._end_turn(actor, turn_key, "interrupted", epoch)

    def prepare_outbound(self, actor, payload, op_id=None):
        """Persist immutable intent before HTTP; attach unresolved automatic provenance."""
        with self._write():
            operation_id = self._prepare_outbound(actor, payload, op_id)
        return self.outbound(operation_id)

    def _prepare_outbound(self, actor, payload, op_id=None):
        """Called within one write transaction, including multi-stage sends."""
        operation_id = op_id or secrets.token_urlsafe(24)
        row = self._require_actor(actor)
        value = dict(payload)
        chains = json.loads(row["causal_json"])
        if chains:
            if value.get("causal") is not None and _chains(value["causal"]) != chains:
                raise LedgerError("Outbound message cannot replace unresolved causal provenance")
            value["causal"] = {"chain_ids": chains}
        encoded = _json(value)
        old = self.db.execute("SELECT actor,payload_json FROM outbound WHERE op_id=?", (operation_id,)).fetchone()
        if old and (old["actor"] != actor or old["payload_json"] != encoded):
            raise LedgerError("Operation ID already binds a different actor or payload")
        now = self.clock()
        self.db.execute("""INSERT OR IGNORE INTO outbound(op_id,actor,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?)""", (operation_id, actor, encoded, now, now))
        return operation_id

    def prepare_attachment_outbound(self, actor, payload, sources):
        """Freeze the original post and all upload IDs atomically before effects."""
        with self._write():
            uploads = [secrets.token_urlsafe(24) for _ in sources]
            parent = self._prepare_outbound(actor, dict(payload, action="attachment_post", uploads=uploads))
            for operation_id, source in zip(uploads, sources):
                self._prepare_outbound(actor, {"action": "upload", "parent_id": parent,
                    "channel_id": payload["channel_id"], "source": source}, operation_id)
        return self.outbound(parent)

    def claim_outbound(self, op_id, *, result=None, admit=None):
        """One possible request, frozen before entry; an uncertain claim is never replayed."""
        with self._write():
            old = self.outbound(op_id)
            if old["state"] != "pending" or admit is not None and not admit():
                return False
            self.db.execute("UPDATE outbound SET state='uncertain',result_json=?,error=?,updated_at=? WHERE op_id=?",
                            (_json(result), "Request outcome not yet confirmed", self.clock(), op_id))
        return True

    def settle_outbound(self, op_id, state, result=None, error=None):
        """Late receipts cannot revive or overwrite a terminal operation."""
        if state not in ("uncertain", "complete", "failed"):
            raise LedgerError("Invalid outbound settlement state")
        if isinstance(result, dict) and ("message" in result or "post" in result):
            raise LedgerError("Store post IDs rather than message bodies in operation results")
        with self._write():
            old = self.outbound(op_id)
            if old["state"] in ("complete", "failed"):
                return False
            self.db.execute("UPDATE outbound SET state=?,result_json=?,error=?,updated_at=? WHERE op_id=?",
                            (state, _json(result), error, self.clock(), op_id))
        return True

    def fail_attachment_outbound(self, op_id, error):
        """Cancel only never-started stages; possible writes remain reconcilable."""
        with self._write():
            operation = self.outbound(op_id)
            if operation["state"] != "pending":
                return
            for target in [op_id, *operation["payload"]["uploads"]]:
                self.db.execute("UPDATE outbound SET state='failed',error=?,updated_at=? WHERE op_id=? AND state='pending'",
                                (error, self.clock(), target))

    def outbound(self, op_id):
        row = self.db.execute("SELECT * FROM outbound WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            raise LedgerError("Unknown outbound operation")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["result"] = json.loads(result.pop("result_json") or "null")
        return result

    def outbounds(self, actor=None):
        query, args = "SELECT op_id FROM outbound", ()
        if actor is not None:
            query, args = query + " WHERE actor=?", (actor,)
        return [self.outbound(r[0]) for r in self.db.execute(query + " ORDER BY created_at,op_id", args)]

    def find_outbound(self, op_id):
        return self.outbound(op_id) if self.db.execute("SELECT 1 FROM outbound WHERE op_id=?", (op_id,)).fetchone() else None

    def unresolved_outbounds(self, actor, limit=None, *, after_op=None):
        query = "SELECT op_id FROM outbound WHERE actor=? AND state IN ('pending','uncertain')"
        args = [actor]
        if after_op is not None:
            query += " AND op_id>?"
            args.append(after_op)
        query += " ORDER BY op_id"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        rows = [self.outbound(row[0]) for row in self.db.execute(query, args)]
        if not rows and after_op is not None:
            return self.unresolved_outbounds(actor, limit)
        return rows

    def unconfirmed_contexts(self, actor, *, limit=None, after_token=None):
        query = """SELECT token FROM attempts WHERE actor=? AND mode='context'
            AND verification_json IS NULL AND transport<>'rejected' AND status IN ('pending','retired')"""
        args = [actor]
        if after_token is not None:
            query += " AND token>?"
            args.append(after_token)
        query += " ORDER BY token"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        tokens = [row[0] for row in self.db.execute(query, args)]
        if not tokens and after_token is not None:
            return self.unconfirmed_contexts(actor, limit=limit)
        return [self.attempt(token) for token in tokens]

    def set_outbound(self, op_id, state, result=None, error=None):
        """Uncertain stays held for server reconciliation; never silently retry it as new work.

        Result is operational metadata (IDs/status), not a canonical message body.
        """
        if state not in ("pending", "uncertain", "complete", "failed"):
            raise LedgerError("Invalid outbound state")
        if isinstance(result, dict) and ("message" in result or "post" in result):
            raise LedgerError("Store post IDs rather than message bodies in operation results")
        with self._write():
            old = self.outbound(op_id)
            if old["state"] == "complete" and (state != "complete" or old["result"] != result):
                raise LedgerError("Completed operation cannot change")
            if old["state"] == "uncertain" and state == "pending":
                raise LedgerError("Uncertain operation must be reconciled before retry")
            self.db.execute("UPDATE outbound SET state=?,result_json=?,error=?,updated_at=? WHERE op_id=?",
                            (state, _json(result), error, self.clock(), op_id))
