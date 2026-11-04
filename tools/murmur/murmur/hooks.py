"""Bounded native hooks. No HTTP, account provisioning or implicit enrollment."""

import json
import sqlite3
import time

from .coordinator import actor_key, identity_dict, routing_binding
from .ledger import LedgerError


CONTROL_LEASE_MS = 10_000


def initialize(db):
    from . import evidence
    evidence.initialize(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS diagnostics(
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT,
            surface TEXT NOT NULL, error TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS runtime_control(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            busy_context_enabled INTEGER NOT NULL, refreshed_at INTEGER NOT NULL,
            actors_json TEXT, delivery_enabled INTEGER NOT NULL DEFAULT 0);
    """)
    if "actors_json" not in {row[1] for row in db.execute("PRAGMA table_info(runtime_control)")}:
        db.execute("ALTER TABLE runtime_control ADD COLUMN actors_json TEXT")
    if "delivery_enabled" not in {row[1] for row in db.execute("PRAGMA table_info(runtime_control)")}:
        db.execute("ALTER TABLE runtime_control ADD COLUMN delivery_enabled INTEGER NOT NULL DEFAULT 0")


def publish_control(ledger, enabled, actors=None, *, delivery_enabled=None):
    """The coordinator grants a short local lease; hooks never read credentials."""
    if actors is not None and (not isinstance(actors, list) or len(actors) > 1000
                              or any(not isinstance(a, str) or not a or len(a) > 160 for a in actors)):
        raise LedgerError("Busy context recipients must be a bounded list of actor IDs")
    selected = json.dumps(sorted(set(actors))) if actors is not None else None
    delivery = enabled is True if delivery_enabled is None else delivery_enabled is True
    ledger.db.execute("""INSERT INTO runtime_control(singleton,busy_context_enabled,refreshed_at,actors_json,delivery_enabled)
        VALUES(1,?,?,?,?)
        ON CONFLICT(singleton) DO UPDATE SET
        busy_context_enabled=excluded.busy_context_enabled,
        refreshed_at=excluded.refreshed_at,actors_json=excluded.actors_json,
        delivery_enabled=excluded.delivery_enabled""",
        (int(enabled is True), ledger.clock(), selected, int(delivery)))


def notifications_enabled(ledger):
    """Use the coordinator's local delivery lease without loading credentials."""
    row = ledger.db.execute("SELECT delivery_enabled,refreshed_at FROM runtime_control WHERE singleton=1").fetchone()
    return bool(row and row["delivery_enabled"] == 1
                and 0 <= ledger.clock() - row["refreshed_at"] <= CONTROL_LEASE_MS)


def busy_context_enabled(ledger, actor=None):
    row = ledger.db.execute("SELECT * FROM runtime_control WHERE singleton=1").fetchone()
    if not row or row["busy_context_enabled"] != 1 or not 0 <= ledger.clock() - row["refreshed_at"] <= CONTROL_LEASE_MS:
        return False
    actors = json.loads(row["actors_json"]) if row["actors_json"] is not None else None
    return actors is None or (bool(actors) if actor is None else actor in actors)


def record_error(ledger, actor, surface, error):
    ledger.db.execute("INSERT INTO diagnostics(actor,surface,error,created_at) VALUES(?,?,?,?)",
                      (actor, surface, str(error)[:2000], int(time.time() * 1000)))


def lineage_evidence(ledger, identity):
    from . import evidence
    return evidence.lineage_evidence(ledger, identity)


def handle(ledger, provider, event, payload):
    from . import native
    initialize(ledger.db)
    ledger.db.execute("PRAGMA busy_timeout=200")
    prompt = str(payload.get("prompt", ""))
    automatic = prompt.startswith(("You have new messages", "Murmur:")) and "[wake:" in prompt
    actor = None
    try:
        if event == "PostToolUse" and not busy_context_enabled(ledger):
            return {}
        captured = native.parse_hook(provider, event, payload)
        if captured is None:
            return {}
        if event == "PostToolUse" and captured.identity.provider == "claude":
            target = captured.identity.target
            if (target.get("platform"), target.get("version"), target.get("runtime")) != ("linux", "2.1.311", "cli"):
                return {}  # Valid unsupported Claude profiles retain idle delivery.
        if captured.creation:
            from .evidence import record_observation
            record_observation(ledger, captured.creation)
        actor = actor_key(captured.identity)
        try:
            registered = ledger.actor(actor)
        except LedgerError:
            # Creation evidence is passive; it grants neither membership nor any
            # injected model context. Only `register` creates enrollment intent.
            if captured.wake_token:
                raise LedgerError("Session is not explicitly enrolled")
            return {}
        if registered.get("mode", "native") == "manual":
            if captured.wake_token:
                raise LedgerError("Manual sidechat enrollment does not accept automatic notifications")
            return {}
        if event == "PostToolUse" and not busy_context_enabled(ledger, actor):
            return {}
        binding = routing_binding(captured.identity)
        fresh = identity_dict(captured.identity)
        # Registration resolves ancestry outside the hook's local boundary.
        if not fresh["parent_known"]:
            for key in ("parent_known", "parent_session_id"):
                fresh[key] = registered["identity"][key]
            fresh["evidence"] = {**registered["identity"].get("evidence", {}), **fresh["evidence"]}
        if captured.identity.provider == "claude":
            # Ordinary native lookups may omit hook-only history facts. Keep
            # them only for the exact owner generation that supplied them.
            previous = registered["identity"]
            same_owner = (captured.identity.epoch is not None
                          and captured.identity.epoch == previous["target"].get("native_epoch")
                          and fresh["target"] == previous["target"])
            for key in ("claude_peer_transcript_path", "claude_peer_engine_version", "claude_peer_capture_epoch"):
                if key not in captured.identity.evidence:
                    fresh["evidence"].pop(key, None)
                    if same_owner and key in previous.get("evidence", {}):
                        fresh["evidence"][key] = previous["evidence"][key]
        if ((captured.identity.provider == "codex" and "desktop_wake" in fresh["evidence"])
                or (captured.identity.provider == "claude"
                    and "claude_peer_transcript_path" in fresh["evidence"])):
            # This existing authorized actor can learn (or clear) its actual
            # client evidence even during a turn begun before an upgrade.
            ledger.refresh_native_evidence(actor, registered["identity"], fresh)
        if captured.event == "session_start" and captured.source == "compact":
            # A parent's enrollment or stale route cannot grant this session
            # context. The hook only returns a cue; the agent checks mail normally.
            ledger._check_identity_refresh(registered["identity"], fresh)
            if (registered["user_id"] and ledger.onboarding_ready(actor)
                    and fresh["target"] == registered["identity"].get("target", {})
                    and notifications_enabled(ledger) and ledger.has_pending_attention(actor)):
                return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                    "Your context was compacted. Check your Murmur inbox when appropriate, then continue your work."}}
        elif captured.event == "submit":
            active = ledger.active_turn(actor)
            if not captured.wake_token and active and active["epoch"] != binding and active.get("identity"):
                # A genuine fresh ordinary Submit can recover a reconnect only
                # when the frozen old owner is positively gone. A new registry
                # entry or failed inspection is insufficient evidence.
                old_owner = native.NativeIdentity(**active["identity"])
                evidence = native.owner_process_state(old_owner, timeout=0.2)
                if evidence["status"] == "ended" and ledger.interrupt(actor, active["turn_key"], active["epoch"]):
                    record_error(ledger, actor, "native_recovery", json.dumps(evidence, sort_keys=True))
            if captured.wake_token:
                attempt = ledger.attempt(captured.wake_token)
                if (attempt["receipt_request"] or {}).get("kind") == "claude-peer-v1":
                    # A busy packet may land after the original task ends.
                    # Submit records this turn; only native peer evidence can
                    # announce the batch or confirm its actual admission.
                    if captured.prompt != (attempt["emission"] or {}).get("text"):
                        raise LedgerError("Claude peer Submit does not match its sealed doorbell")
                    ledger.record_peer_submit(captured.wake_token, actor, binding, captured.turn_key,
                                               native_identity=fresh)
                else:
                    ledger.admit(captured.wake_token, actor, binding, captured.turn_key,
                                 supersede=True, native_identity=fresh)
            else:
                ledger.start_turn(actor, binding, captured.turn_key,
                                  supersede=True, native_identity=fresh)
        elif captured.event == "post_tool":
            if fresh["target"] != registered["identity"].get("target", {}):
                raise LedgerError("Native tool target differs from the enrolled route; verify registration")
            # A hook can establish activity after mid-turn enrollment. It cannot
            # supersede a different turn or resurrect a completed one.
            ledger.start_turn(actor, binding, captured.turn_key, native_identity=fresh)
            if not ledger.notification_candidates(actor, limit=1):
                return {}
            request = native.prepare_context_receipt(captured)
            attempt = ledger.claim_context(actor, binding, captured.turn_key, captured.tool_id,
                                           native_identity=fresh, receipt_request=request)
            if not attempt:
                return {}
            names = {chat["id"]: chat["name"] for chat in ledger.conversations(actor)}
            message = native.format_doorbell([names.get(channel, "a conversation")
                                              for channel in attempt["channels"]], attempt["token"])
            if not ledger.mark_context_emitted(attempt["token"], actor, binding, captured.turn_key,
                                               captured.tool_id, {"context": message}):
                return {}
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}
        elif captured.event == "complete":
            ledger.complete(actor, captured.turn_key, binding)
        elif captured.event == "interrupt":
            ledger.interrupt(actor, captured.turn_key, binding)
        # Session start/end is not proof that an arbitrary active turn finished.
        return {}
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        try:
            record_error(ledger, actor, "hook", error)
        except sqlite3.Error:
            pass  # A locked ledger must still return a blocking doorbell result.
        if automatic and event == "UserPromptSubmit":
            return {"decision": "block", "reason": "Murmur notification held: " + str(error)}
        return {}
