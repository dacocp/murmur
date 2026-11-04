"""Indexed native creation evidence; no enrollment, transport or backend policy.

Raw observations and certificates remain immutable audit records. Legacy rows
are indexed in resumable batches during registration, never inside a hook.
"""
from __future__ import annotations

import json
import time

from .ledger import LedgerError
from .native import lineage


_BATCH = 256
_MAX_BYTES = 1024 * 1024


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS native_observations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence TEXT NOT NULL UNIQUE, captured_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS native_proofs(
            actor TEXT PRIMARY KEY, evidence TEXT NOT NULL);
    """)
    additions = {
        "native_observations": (("session_id", "TEXT"), ("process_key", "TEXT"),
                                ("prompt_id", "TEXT"), ("event", "TEXT"), ("source", "TEXT"),
                                ("index_error", "TEXT"), ("indexed", "INTEGER NOT NULL DEFAULT 0")),
        "native_proofs": (("process_key", "TEXT"), ("prompt_id", "TEXT"),
                          ("index_error", "TEXT"), ("indexed", "INTEGER NOT NULL DEFAULT 0")),
    }
    for table, columns in additions.items():
        present = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
        for name, kind in columns:
            if name not in present:
                db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + kind)


def _indexes(db):
    # Creating an index scans legacy rows, so registration owns this migration.
    db.executescript("""
        CREATE INDEX IF NOT EXISTS native_observation_session ON native_observations(session_id,id);
        CREATE INDEX IF NOT EXISTS native_observation_transition ON native_observations(process_key,prompt_id,id);
        CREATE INDEX IF NOT EXISTS native_observation_unindexed ON native_observations(indexed,id);
        CREATE INDEX IF NOT EXISTS native_observation_unaddressable ON native_observations(id)
            WHERE index_error IS NOT NULL AND session_id IS NULL AND process_key IS NULL;
        CREATE INDEX IF NOT EXISTS native_proof_transition ON native_proofs(process_key,prompt_id);
        CREATE INDEX IF NOT EXISTS native_proof_unindexed ON native_proofs(indexed,actor);
    """)


def _decode(raw):
    if not isinstance(raw, str) or len(raw.encode()) > _MAX_BYTES:
        raise LedgerError("Native evidence exceeds its record bound")
    value = json.loads(raw, object_pairs_hook=lineage._object)
    if not isinstance(value, dict):
        raise LedgerError("Native evidence must be an object")
    return value


def _process(value):
    return json.dumps(lineage.process_key(value), separators=(",", ":"))


def _observation_fields(raw):
    session = process = prompt = event = source = error = None
    try:
        value = _decode(raw)
        payload = value.get("payload", {})
        # Retain independently valid scope even when the other address field is
        # malformed. That process must still see its failed provenance.
        try:
            process = _process(value.get("process"))
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc)[:1000]
        if isinstance(payload, dict):
            session = lineage.canonical_uuid(payload.get("session_id"))
            event, source = payload.get("hook_event_name"), payload.get("source")
            prompt = payload.get("prompt_id")
        clean = lineage.observation(payload, value["process"])
        if clean is None or clean != value:
            raise LedgerError("Native creation observation changed")
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        error = str(exc)[:1000]
    event = event if isinstance(event, str) else None
    source = source if isinstance(source, str) else None
    prompt = prompt if isinstance(prompt, str) else None
    return session, process, prompt, event, source, error


def _proof_fields(raw):
    process = prompt = error = None
    try:
        value = _decode(raw)
        lineage._known_records([value])
        process = _process(value)
        prompt = value.get("prompt_id")
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        error = str(exc)[:1000]
    return process, prompt, error


def record_observation(ledger, observation):
    """Append an adapter-validated capture without scanning historical rows."""
    raw = json.dumps(observation, sort_keys=True)
    fields = _observation_fields(raw)
    if fields[-1]:
        raise LedgerError(fields[-1])
    ledger.db.execute("""INSERT OR IGNORE INTO native_observations(
        evidence,captured_at,session_id,process_key,prompt_id,event,source,index_error,indexed)
        VALUES(?,?,?,?,?,?,?,?,1)""", (raw, ledger.clock(), *fields))


def _migrate(ledger, deadline):
    """Index old audit rows in committed batches; a retry resumes its progress."""
    for table, key, fields in (("native_observations", "id", _observation_fields),
                               ("native_proofs", "actor", _proof_fields)):
        while True:
            rows = list(ledger.db.execute("SELECT " + key + ",evidence FROM " + table
                                          + " WHERE indexed=0 ORDER BY " + key + " LIMIT ?", (_BATCH,)))
            if not rows:
                break
            if time.monotonic() >= deadline:
                raise LedgerError("Native evidence indexing is pending; retry registration to resume")
            columns = ("session_id,process_key,prompt_id,event,source,index_error" if key == "id"
                       else "process_key,prompt_id,index_error")
            assignments = ",".join(name + "=?" for name in columns.split(","))
            with ledger._write():
                for row in rows:
                    ledger.db.execute("UPDATE " + table + " SET " + assignments + ",indexed=1 WHERE " + key + "=?",
                                      (*fields(row[1]), row[0]))


def persist_proofs(ledger, certificates):
    """Retain exact immutable certificates, indexing them for scoped reuse."""
    certificates = list(certificates)
    if len(certificates) > lineage.MAX_ANCESTRY_IDS:
        raise LedgerError("Native certified ancestry exceeds its bound")
    with ledger._write():
        for certificate in certificates:
            raw = json.dumps(certificate, sort_keys=True)
            fields = _proof_fields(raw)
            if fields[-1]:
                raise LedgerError(fields[-1])
            actor = "claude:" + certificate["session_id"]
            previous = ledger.db.execute("SELECT evidence FROM native_proofs WHERE actor=?", (actor,)).fetchone()
            if previous and previous[0] != raw:
                raise LedgerError("Certified native ancestry conflicts with the recorded proof")
            if fields[1] is not None and ledger.db.execute("""SELECT 1 FROM native_proofs
                    WHERE process_key=? AND prompt_id=? AND actor<>? LIMIT 1""",
                    (fields[0], fields[1], actor)).fetchone():
                raise LedgerError("Certified native transition was already consumed by another child")
            ledger.db.execute("""INSERT OR IGNORE INTO native_proofs(
                actor,evidence,process_key,prompt_id,index_error,indexed) VALUES(?,?,?,?,?,1)""",
                (actor, raw, *fields))


def lineage_evidence(ledger, identity, *, timeout=3):
    """Select the target's proof closure and its exact creation transitions.

    Failed captures for a required session remain inputs to certification. Other
    sessions/processes do not consume this proof's observation bound. Competing
    children of a selected transition remain visible, including certified ones.
    """
    if identity.provider != "claude" or identity.target.get("runtime") != "cli":
        return {"observations": [], "known": []}
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 60:
        raise LedgerError("Native evidence timeout must be finite and positive")
    initialize(ledger.db)
    deadline = time.monotonic() + timeout
    _indexes(ledger.db)
    _migrate(ledger, deadline)
    if ledger.db.execute("""SELECT 1 FROM native_observations WHERE index_error IS NOT NULL
            AND session_id IS NULL AND process_key IS NULL LIMIT 1""").fetchone():
        # Without either exact address, no recipient can exclude relevance.
        # Retain the failed audit row rather than guessing it was unrelated.
        raise LedgerError("Native creation evidence has an unaddressable failed capture; retained for inspection")
    observations, known, sessions, transitions = {}, {}, set(), set()
    failed_processes = set()
    pending = [identity.session_id]

    def check():
        if time.monotonic() >= deadline:
            raise LedgerError("Relevant native creation evidence inspection exceeded its deadline")
        if len(observations) > lineage.MAX_OBSERVATIONS:
            raise LedgerError("Relevant native creation evidence exceeds its bound; audit evidence was retained")
        if len(sessions) > lineage.MAX_ANCESTRY_IDS:
            raise LedgerError("Relevant native ancestry exceeds its bound")
        if len(known) > lineage.MAX_ANCESTRY_IDS:
            raise LedgerError("Relevant native certificates exceed their bound")

    def add_proof(row):
        check()
        if row["index_error"]:
            raise LedgerError("Required native certificate is malformed: " + row["index_error"])
        value = _decode(row["evidence"])
        session = lineage.canonical_uuid(value.get("session_id"))
        if row["actor"] != "claude:" + session:
            raise LedgerError("Native certificate address disagrees with its session")
        known[session] = value
        add_failed_process(row["process_key"])
        parent = value.get("parent_session_id")
        if parent is not None and parent not in sessions:
            pending.append(parent)

    def add_observation(row):
        observations[row["id"]] = _decode(row["evidence"])
        check()
        # Only a fork start needs its paired SessionEnd. Following every end
        # would pull unrelated sibling branches into the target's proof.
        if row["event"] != "SessionStart" or row["source"] != "fork":
            return
        transition = row["process_key"], row["prompt_id"]
        if None in transition or transition in transitions:
            return
        transitions.add(transition)
        for paired in ledger.db.execute("""SELECT * FROM native_observations
                WHERE process_key=? AND prompt_id=? ORDER BY id LIMIT ?""",
                (*transition, lineage.MAX_OBSERVATIONS + 1)):
            observations[paired["id"]] = _decode(paired["evidence"])
            if paired["event"] == "SessionEnd" and paired["session_id"] not in sessions:
                pending.append(paired["session_id"])
            check()
        for proof in ledger.db.execute("SELECT * FROM native_proofs WHERE process_key=? AND prompt_id=? LIMIT ?",
                                       (*transition, lineage.MAX_ANCESTRY_IDS + 1)):
            add_proof(proof)

    def add_failed_process(process):
        if process is None or process in failed_processes:
            return
        failed_processes.add(process)
        for row in ledger.db.execute("""SELECT * FROM native_observations
            WHERE process_key=? AND index_error IS NOT NULL ORDER BY id LIMIT ?""",
            (process, lineage.MAX_OBSERVATIONS + 1)):
            add_observation(row)

    add_failed_process(_process(identity.target))

    while pending:
        check()
        session = pending.pop()
        if session in sessions:
            continue
        lineage.canonical_uuid(session)
        sessions.add(session)
        proof = ledger.db.execute("SELECT * FROM native_proofs WHERE actor=?", ("claude:" + session,)).fetchone()
        if proof:
            add_proof(proof)
        for row in ledger.db.execute("""SELECT * FROM native_observations WHERE session_id=?
            AND (index_error IS NOT NULL OR (event='SessionStart'
                 AND (source IS NULL OR source NOT IN ('resume','compact','clear'))))
            ORDER BY id LIMIT ?""", (session, lineage.MAX_OBSERVATIONS + 1)):
            add_observation(row)
    check()
    return {"observations": [observations[key] for key in sorted(observations)],
            "known": list(known.values())}
