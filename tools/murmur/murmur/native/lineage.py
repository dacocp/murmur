"""Pure Claude CLI creation proofs; persistence belongs to the coordinator.

Only observations captured by the native adapter and proofs previously returned
here are trusted inputs. The caller must retain them immutably and in capture
order. No process environment, transcript text or registration state is stored.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .types import IdentityError

SUPPORTED_VERSION = "2.1.310"
SUPPORTED_LINUX_VERSION = "2.1.311"
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_ANCESTRY_IDS = 128
MAX_OBSERVATIONS = 4096
SOURCES = {"startup", "resume", "fork", "clear", "compact"}
REASONS = {"resume", "clear", "logout", "prompt_input_exit", "other", "exit"}


def canonical_uuid(value, field="session_id"):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise IdentityError(f"Claude CLI lineage unresolved: invalid {field}") from exc
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise IdentityError("Duplicate JSON field in native proof")
        value[key] = item
    return value


def process_key(process):
    if (not isinstance(process, dict) or type(process.get("pid")) is not int
            or not 1 < process["pid"] < 2**31
            or not isinstance(process.get("proc_start"), str)
            or not process["proc_start"].strip() or len(process["proc_start"]) > 256
            or process.get("entrypoint") != "cli"):
        raise IdentityError("Claude CLI lineage unresolved: verified pinned process birth required")
    platform = process.get("platform", "darwin")
    if platform == "darwin" and process.get("version") == SUPPORTED_VERSION:
        if process.get("pid_domain", "darwin") != "darwin":
            raise IdentityError("Claude CLI Darwin lineage has a foreign PID domain")
        return process["pid"], process["proc_start"]
    domain = process.get("pid_domain")
    if (platform != "linux" or process.get("version") != SUPPORTED_LINUX_VERSION
            or not isinstance(domain, str)
            or not re.fullmatch(r"linux:[0-9a-f]{32}:pid:\[[0-9]{1,20}\]", domain)
            or not process["proc_start"].isascii() or not process["proc_start"].isdecimal()):
        raise IdentityError("Claude CLI lineage unresolved: verified pinned platform and PID domain required")
    return process["pid"], process["proc_start"], domain


def _process_fields(process):
    fields = {key: process[key] for key in ("pid", "proc_start", "entrypoint", "version")}
    if process.get("platform") == "linux":
        fields.update(platform="linux", pid_domain=process["pid_domain"])
    return fields


def observation(payload, process):
    """Select bounded creation fields after the adapter verifies the invoker."""
    if not isinstance(payload, dict):
        raise IdentityError("Claude CLI creation payload must be an object")
    if payload.get("agent_id"):
        return None
    event = payload.get("hook_event_name")
    if event not in {"SessionStart", "SessionEnd"}:
        return None
    process_key(process)
    session = canonical_uuid(payload.get("session_id"))
    selected = {"hook_event_name": event, "session_id": session}
    for field, choices in (("source", SOURCES), ("reason", REASONS)):
        item = payload.get(field)
        if item is not None:
            if not isinstance(item, str) or item not in choices:
                raise IdentityError(f"Claude CLI creation has unsupported {field}")
            selected[field] = item
    if payload.get("prompt_id") is not None:
        selected["prompt_id"] = canonical_uuid(payload["prompt_id"], "prompt_id")
    path = payload.get("transcript_path")
    if path is not None:
        if (not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute()
                or Path(path).name != f"{session}.jsonl"):
            raise IdentityError("Claude CLI creation transcript address is invalid")
        selected["transcript_path"] = path
    facts = _process_fields(process)
    facts["root_start_verified"] = process.get("root_start_verified") is True
    for key in ("config_dir", "home_dir"):
        if key in process:
            if (not isinstance(process[key], str) or len(process[key]) > 4096
                    or not Path(process[key]).is_absolute()):
                raise IdentityError("Claude CLI creation routing address is invalid")
            facts[key] = process[key]
    result = {"payload": selected, "process": facts}
    result["event_key"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def transcript_proof(path_value, config_dir, child, parent):
    """Check the exact bounded native child metadata; never compare message text."""
    try:
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise ValueError
        path = Path(path_value)
        if str(path.resolve(strict=True)) != path_value or path.name != f"{child}.jsonl":
            raise ValueError
        config = Path(config_dir)
        if not config.is_absolute():
            raise ValueError
        projects = config.resolve(strict=True) / "projects"
        if projects.resolve(strict=True) != projects:
            raise ValueError
        relative = path.relative_to(projects)
        if len(relative.parts) < 2:
            raise ValueError
        directory = os.open(projects, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in relative.parts[:-1]:
                nested = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=directory)
                os.close(directory)
                directory = nested
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o022 or before.st_size <= 0):
                raise ValueError
            if before.st_size > MAX_TRANSCRIPT_BYTES:
                raise IdentityError("Claude CLI transcript proof exceeds 32 MiB supported limit")
            total, copied = 0, 0
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > MAX_LINE_BYTES:
                    raise IdentityError("Claude CLI transcript proof exceeds 2 MiB line limit")
                if total > MAX_TRANSCRIPT_BYTES:
                    raise IdentityError("Claude CLI transcript proof exceeds 32 MiB supported limit")
                if not line.endswith(b"\n"):
                    raise ValueError
                row = json.loads(line, object_pairs_hook=_object)
                if not isinstance(row, dict):
                    raise ValueError
                message = row.get("type") in {"user", "assistant"}
                if (message or "sessionId" in row) and row.get("sessionId") != child:
                    raise ValueError
                if "forkedFrom" in row:
                    origin = row["forkedFrom"]
                    if (not isinstance(origin, dict)
                            or canonical_uuid(origin.get("sessionId"), "fork parent") != parent):
                        raise ValueError
                    copied += int(message)
            after, latest = os.fstat(stream.fileno()), path.lstat()
            signature = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                      item.st_mtime_ns, item.st_ctime_ns)
            if (not copied or total != before.st_size or signature(before) != signature(after)
                    or signature(before) != signature(latest)
                    or not stat.S_ISREG(latest.st_mode) or latest.st_uid != os.getuid()
                    or latest.st_mode & 0o022):
                raise ValueError
    except IdentityError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise IdentityError("Claude CLI child transcript proof is missing, unsafe or inconsistent") from exc
    return path_value


def _known_records(known):
    records = {}
    for item in known:
        if not isinstance(item, dict):
            raise IdentityError("Claude CLI certified proof is malformed")
        session = canonical_uuid(item.get("session_id"))
        parent = item.get("parent_session_id")
        if parent is not None:
            canonical_uuid(parent, "parent_session_id")
        canonical_uuid(item.get("root_session_id"), "root_session_id")
        process_key(item)
        if item.get("proof") not in {"startup", "branch"}:
            raise IdentityError("Claude CLI certified proof kind is unsupported")
        if (item["proof"] == "startup") != (parent is None):
            raise IdentityError("Claude CLI certified proof parent is inconsistent")
        if item["proof"] == "branch":
            canonical_uuid(item.get("prompt_id"), "prompt_id")
            if not isinstance(item.get("transcript_path"), str):
                raise IdentityError("Claude CLI certified branch lacks its transcript address")
        selected = {key: item.get(key) for key in (
            "session_id", "parent_session_id", "root_session_id", "proof", "pid", "proc_start",
            "entrypoint", "version", "prompt_id", "transcript_path")}
        selected.update(_process_fields(item))
        if session in records and records[session] != selected:
            raise IdentityError("Claude CLI certified lineage conflicts with immutable proof")
        records[session] = selected
    return records


def _closure(records, session):
    result, seen, root = [], set(), None
    while session is not None:
        if session in seen or len(seen) >= MAX_ANCESTRY_IDS:
            raise IdentityError("Claude CLI certified ancestry cycles or exceeds 128 sessions")
        seen.add(session)
        record = records.get(session)
        if not record:
            raise IdentityError("Claude CLI target lacks complete certified ancestry")
        root = root or record["root_session_id"]
        if record["root_session_id"] != root:
            raise IdentityError("Claude CLI certified ancestry has conflicting roots")
        result.append(record)
        session = record["parent_session_id"]
    if result[-1]["session_id"] != root or result[-1]["proof"] != "startup":
        raise IdentityError("Claude CLI target lacks a certified native root")
    return result


def certify(session_id, *, observations=(), known=(), config_dir, process):
    """Certify typed /branch evidence against a fresh fully bound current process.

    Caller persists the returned root-first `ancestors` immutably. A cold resume
    may use those known proofs under a new process epoch. A previously unknown
    startup can only be certified by that startup's own first current identity;
    a later different engine cannot turn an old startup into a guessed root.
    Failed observations in the target's ancestry remain failures; unrelated
    observations do not invalidate an otherwise certified target.
    """
    canonical_uuid(session_id)
    current_epoch = process_key(process)
    observations = list(observations)
    if len(observations) > MAX_OBSERVATIONS:
        raise IdentityError("Claude CLI creation proof exceeds observation bound")
    records = _known_records(known)
    transitions, failures, seen_events = {}, {}, set()
    # Certified children retain one-shot consumption even when the coordinator
    # only supplies newly captured events on a later call.
    for item in records.values():
        if item["proof"] != "branch":
            continue
        key = (*process_key(item), item["prompt_id"])
        pair = [item["parent_session_id"], item["session_id"]]
        if key in transitions and transitions[key] != pair:
            raise IdentityError("Claude CLI certified proofs reuse a consumed transition")
        transitions[key] = pair
    for observed in observations:
        session = None
        try:
            if not isinstance(observed, dict):
                raise IdentityError("Claude CLI creation observation is malformed")
            clean = observation(observed.get("payload"), observed.get("process"))
            if clean is None or clean != observed:
                raise IdentityError("Claude CLI immutable observation changed")
            if clean["event_key"] in seen_events:
                continue
            seen_events.add(clean["event_key"])
            payload, facts = clean["payload"], clean["process"]
            session = payload["session_id"]
            epoch = process_key(facts)
            if epoch != current_epoch and session not in records:
                # Old certified ancestry is supplied as known proofs. Raw old
                # observations cannot certify themselves under a new process.
                raise IdentityError("Claude CLI creation belongs to an unverified process epoch")
            event, source = payload["hook_event_name"], payload.get("source")
            if event == "SessionEnd":
                if payload.get("reason") != "resume":
                    continue
                prompt = canonical_uuid(payload.get("prompt_id"), "prompt_id")
                if session not in records:
                    raise IdentityError("Claude CLI branch parent has no certified lineage")
                _closure(records, session)
                key = (*epoch, prompt)
                previous = transitions.get(key)
                if previous and previous[0] != session:
                    raise IdentityError("Claude CLI branch transition has conflicting parents")
                transitions.setdefault(key, [session, None])
                continue
            if source == "startup":
                if (uuid.UUID(session).version != 4 or not facts["root_start_verified"]
                        or (session not in records and
                            (not process.get("root_start_verified") or session != session_id))):
                    raise IdentityError("Claude CLI plain startup was not verified by its own current engine")
                candidate = dict(session_id=session, parent_session_id=None, root_session_id=session,
                                 proof="startup", **_process_fields(facts),
                                 prompt_id=None, transcript_path=None)
            elif source in {"resume", "compact", "clear"}:
                if session not in records:
                    raise IdentityError(f"Claude CLI {source} has no certified lineage")
                continue
            elif source == "fork":
                prompt = canonical_uuid(payload.get("prompt_id"), "prompt_id")
                pair = transitions.get((*epoch, prompt))
                if not pair:
                    raise IdentityError("Claude CLI fork lacks its preceding native SessionEnd")
                parent, consumed = pair
                if session == parent or (consumed is not None and consumed != session):
                    raise IdentityError("Claude CLI branch transition is conflicting or consumed")
                ancestors = _closure(records, parent)
                path = payload.get("transcript_path")
                previous = records.get(session)
                # A certified proof is immutable identity metadata. Replaying
                # its identical capture must not rescan a transcript that has
                # subsequently grown, moved or been compacted by Claude.
                if not previous:
                    path = transcript_proof(path, config_dir, session, parent)
                candidate = dict(session_id=session, parent_session_id=parent,
                                 root_session_id=ancestors[-1]["session_id"], proof="branch",
                                 **_process_fields(facts),
                                 prompt_id=prompt, transcript_path=path)
                pair[1] = session
            else:
                raise IdentityError("Claude CLI session start source is unsupported")
            if session in records and records[session] != candidate:
                raise IdentityError("Claude CLI creation conflicts with immutable certified lineage")
            records[session] = candidate
        except (ValueError, TypeError, KeyError) as exc:
            # Without even an exact failure identity, accepting any target would
            # hide potentially relevant failed provenance.
            if session is None:
                raise IdentityError(f"Claude CLI creation proof unresolved: {exc}") from exc
            failures.setdefault(session, str(exc))
    if session_id not in records and session_id in failures:
        raise IdentityError(f"Claude CLI creation proof unresolved: {failures[session_id]}")
    ancestors = _closure(records, session_id)
    for item in ancestors:
        if (item.get("platform", "darwin"), item.get("pid_domain", "darwin")) != (
                process.get("platform", "darwin"), process.get("pid_domain", "darwin")):
            raise IdentityError("Claude CLI ancestry belongs to another native platform or PID domain")
        if item["session_id"] in failures:
            raise IdentityError(f"Claude CLI creation proof unresolved: {failures[item['session_id']]}")
    return {**records[session_id], "ancestors": list(reversed(ancestors))}
