"""Bounded receipts for native Codex hook context, independent of the ledger.

The measured builds retain canonical JSONL even in paginated history mode.
SQLite's typed UI projection omits developer context. A receipt proves insertion
into native history, not model execution, reading mail, or power-loss durability.
Only locally exercised backend executables are admitted here.
Tool attribution is the trusted hook capture's association; the native developer
message itself has no tool ID.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from .hooks import parse_wake
from .files import executable_sha256
from .types import HookEvent, NativeError, NativeIdentity

_MAX_FILE = 256 * 1024 * 1024  # Legacy schema-1 full snapshots only.
# Long histories need a larger total budget without widening the decoder's
# separate per-record budget. These are reader limits, not native maxima.
_MAX_LINE = 8 * 1024 * 1024
_MAX_DEPTH = 128
_CAPTURE_TIMEOUT = 1.0
_PROFILES = {
    "b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306": "0.164.0",
    "1afb28c01d7d49e892bd75f11076b10f1451e34e0ee308a95e5ab7f69f9c84fe": "0.163.0-alpha.10.2",
    "d8990e3ee7acfbc26f91265b12a1a83f0fee40d0e0f8f1f40a37d4ec21f499cf": "0.164.0",
    "a04752e4d46a4d89e53e6f61c59c5349a638c8ce172ffbf391fd482b4d902a4e": "0.164.0-alpha.11.1",
    "d84a54b031085f01e3ad915ea7c72ebea4dcf69e90f0d1e0e4a637e8280991d2": "0.165.0-alpha.1.2",
    "58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea": "0.166.0-alpha.5.1",
    "508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44": "0.166.0-alpha.6",
    "a636331ed87605efc4cf84028ac2884a75363f9d10ead63cffb57ea0016d486e": "0.167.0-alpha.3.2",
    "7cb8700a47be3c81bb3c5efbf2d2adc716ce3db4806e9dc992f2925f78b2b194": "0.167.0-alpha.4.1",
    "3ea20787e106af94573d3d6c19480aed93a1334b0dd32b6b04e2f780ce82cf4b": "0.169.0-alpha.11.1",
}
# Qualified event writers check the historical header's schema; its creator
# label is diagnostic metadata, not the identity of the current append owner.
_STRUCTURAL_EVENT_PROFILES = frozenset({
    "508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44",
    "a636331ed87605efc4cf84028ac2884a75363f9d10ead63cffb57ea0016d486e",
    "7cb8700a47be3c81bb3c5efbf2d2adc716ce3db4806e9dc992f2925f78b2b194",
    "3ea20787e106af94573d3d6c19480aed93a1334b0dd32b6b04e2f780ce82cf4b",
})
# Earlier writer profiles retain their exercised append-owner/header pairs.
_CREATION_HEADER_COMPATIBILITY = {
    "d84a54b031085f01e3ad915ea7c72ebea4dcf69e90f0d1e0e4a637e8280991d2": ("0.164.0-alpha.11.1",),
    "58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea": ("0.164.0-alpha.11.1",),
}
_LEGACY_PROVENANCE_PROFILES = frozenset({
    "1afb28c01d7d49e892bd75f11076b10f1451e34e0ee308a95e5ab7f69f9c84fe",
})
_KIND = "codex-canonical-jsonl"
_EVENT_KIND = "codex-canonical-event-jsonl"
_MAX_EVENT_TAIL = 16 * 1024 * 1024
_MAX_EVENT_OFFSET = (1 << 63) - 1
_EVENT_CHUNK = 64 * 1024


def _check_time(deadline):
    if time.monotonic() >= deadline:
        raise NativeError("Native context inspection exceeded its deadline")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise NativeError("Duplicate native context JSON field")
        result[key] = value
    return result


def _constant(value):
    raise NativeError("Non-JSON constant in native context: " + value)


def _decode(line, deadline):
    _check_time(deadline)
    if len(line) > _MAX_LINE:
        raise NativeError("Native context line exceeds the supported limit")
    try:
        value = json.loads(line, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise NativeError("Malformed native context JSON") from exc
    _check_time(deadline)
    # Traverse wide replacement histories without allocating a second stack
    # containing every child. The traversal stack is bounded by JSON depth.
    stack, visited = [(iter((value,)), 0)], 0
    while stack:
        children, depth = stack[-1]
        try:
            item = next(children)
        except StopIteration:
            stack.pop()
            continue
        if depth > _MAX_DEPTH:
            raise NativeError("Native context JSON exceeds the supported depth")
        if isinstance(item, dict):
            stack.append((iter(item.values()), depth + 1))
        elif isinstance(item, list):
            stack.append((iter(item), depth + 1))
        visited += 1
        if visited % 1024 == 0:
            _check_time(deadline)
    if not isinstance(value, dict):
        raise NativeError("Native context record must be an object")
    _check_time(deadline)
    return value


def _line_spans(data, deadline, *, start=0):
    """Yield complete JSONL byte spans without copying the remaining history."""
    while start < len(data):
        _check_time(deadline)
        newline = data.find(b"\n", start)
        if newline < 0:
            return  # Native append can still be in progress.
        if newline - start > _MAX_LINE:
            raise NativeError("Native context line exceeds the supported limit")
        yield start, newline + 1
        start = newline + 1


def _profile(profile):
    if (not isinstance(profile, dict)
            or profile.get("sha256") not in _PROFILES
            or _PROFILES[profile["sha256"]] != profile.get("version")
            or type(profile.get("uid")) is not int or profile["uid"] != os.getuid()
            or type(profile.get("pid")) is not int or profile["pid"] <= 0
            or not isinstance(profile.get("birth"), str) or not profile["birth"]
            or not isinstance(profile.get("executable"), str)
            or not Path(profile["executable"]).is_absolute()):
        raise NativeError("Unsupported native Codex context profile")
    return profile


def context_profile_version(sha256):
    """Return a certified context version for an exact executable hash only."""
    return _PROFILES.get(sha256)


def inspect_context_owner(*, timeout=0.5):
    """Measure the nearest invoking owner without certifying an unknown build."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise NativeError("Native context timeout must be finite and positive")
    return _inspect_owner_profile(time.monotonic() + timeout)


def _owner_profile(deadline):
    """Certify the nearest invoking Codex backend, never another owner above it."""
    return _profile(_inspect_owner_profile(deadline))


def _inspect_owner_profile(deadline):
    from . import process
    pid = os.getppid()
    seen = set()
    for _ in range(16):
        _check_time(deadline)
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        before = process.identity(pid, timeout=min(0.2, deadline - time.monotonic()))
        if before[1] != os.getuid():
            raise NativeError("Native context owner has a different OS user")
        executable, argv = process.inspect(pid)
        if Path(executable).name.lower() == "codex":
            if not argv or Path(argv[0]).name.lower() != "codex":
                raise NativeError("Native context owner arguments are ambiguous")
            sha = executable_sha256(executable, deadline)
            after = process.identity(pid, timeout=min(0.2, deadline - time.monotonic()))
            if before != after:
                raise NativeError("Native context owner changed during inspection")
            _check_time(deadline)
            return {"pid": pid, "uid": before[1], "birth": before[2],
                    "executable": executable, "sha256": sha,
                    "version": _PROFILES.get(sha)}
        pid = before[0]
    raise NativeError("Native context has no certified invoking Codex owner")


def _hook_provenance(value):
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    if value[0].keys() != {"provenance"}:
        return False
    provenance = value[0]["provenance"]
    return (isinstance(provenance, dict) and provenance.keys() == {"type", "hook"}
            and provenance["type"] == "harness" and provenance["hook"] is True)


def _path(identity, value):
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise NativeError("Native context requires an absolute transcript path")
    environment = identity.target.get("env", {})
    home = environment.get("CODEX_HOME")
    if not home:
        root = environment.get("HOME")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise NativeError("Native context lacks its captured Codex home")
        home = str(Path(root) / ".codex")
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise NativeError("Native Codex home must be absolute")
    path = Path(value)
    if path.is_symlink():
        raise NativeError("Native context file cannot be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to((Path(home) / "sessions").resolve(strict=True)):
        raise NativeError("Native context lies outside its captured session store")
    return resolved


def _snapshot(path, deadline):
    """Read one bounded regular file, checking that its pathname still names it."""
    _check_time(deadline)
    initial = path.lstat()
    if (not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.getuid()
            or initial.st_mode & 0o022 or initial.st_size > _MAX_FILE):
        raise NativeError("Native context file ownership, mode, or size is unsupported")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise NativeError("Native context file changed while opening")
        blocks, remaining = [], initial.st_size
        while remaining:
            _check_time(deadline)
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                raise NativeError("Native context file was truncated during inspection")
            blocks.append(block)
            remaining -= len(block)
        current = path.lstat()
        final = os.fstat(fd)
        if ((current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino)
                or not stat.S_ISREG(current.st_mode) or current.st_mode & 0o022
                or current.st_uid != os.getuid() or final.st_size < initial.st_size):
            raise NativeError("Native context file was replaced or truncated")
        return b"".join(blocks), initial
    finally:
        os.close(fd)


def _header(data, identity, profile, deadline):
    newline = data.find(b"\n")
    if newline < 0:
        raise NativeError("Native context header is incomplete")
    first = data[:newline]
    record = _decode(first, deadline)
    header = record.get("payload")
    creation = header.get("cli_version") if isinstance(header, dict) else None
    if profile["sha256"] in _STRUCTURAL_EVENT_PROFILES:
        creation_valid = (isinstance(creation, str) and 1 <= len(creation) <= 128
                          and creation.strip() == creation and creation.isprintable())
    else:
        creation_valid = creation in (profile["version"], *_CREATION_HEADER_COMPATIBILITY.get(profile["sha256"], ()))
    if (type(record.get("ordinal")) is not int or record["ordinal"] < 0
            or record.get("type") != "session_meta" or not isinstance(header, dict)
            or header.get("id") != identity.session_id
            or header.get("session_id") != identity.session_id
            or not creation_valid
            or header.get("history_mode") != "paginated"):
        raise NativeError("Native context header does not match its certified recipient/profile")
    return hashlib.sha256(first).hexdigest(), record["ordinal"]


def _pread_exact(descriptor, start, length, deadline):
    blocks = []
    while length:
        _check_time(deadline)
        block = os.pread(descriptor, min(length, _EVENT_CHUNK), start)
        if not block:
            raise NativeError("Native context event window was truncated")
        blocks.append(block)
        start += len(block)
        length -= len(block)
    _check_time(deadline)
    return b"".join(blocks)


def _seal_window(windows, start, data):
    windows.append((start, len(data), hashlib.sha256(data).hexdigest()))


@contextmanager
def _event_source(path, deadline):
    """Seal only the read windows, allowing unrelated later appends.

    Neither file metadata nor these windows certify unread historical bytes.
    The tail limit bounds scan extent; rechecking seals adds bounded I/O.
    """
    _check_time(deadline)
    initial = path.lstat()
    if (not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.getuid()
            or initial.st_mode & 0o022 or not 0 <= initial.st_size <= _MAX_EVENT_OFFSET):
        raise NativeError("Native context event file ownership, mode, or size is unsupported")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or opened.st_mode & 0o022 or not 0 <= opened.st_size <= _MAX_EVENT_OFFSET):
            raise NativeError("Native context event file changed while opening")
        windows = []
        yield descriptor, opened, windows
        for start, length, expected in windows:
            digest = hashlib.sha256()
            while length:
                block = _pread_exact(descriptor, start, min(length, _EVENT_CHUNK), deadline)
                digest.update(block)
                start += len(block)
                length -= len(block)
            if digest.hexdigest() != expected:
                raise NativeError("Native context event header, anchor, or receipt window changed")
        current, final = path.lstat(), os.fstat(descriptor)
        if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or current.st_mode & 0o022 or final.st_size < opened.st_size):
            raise NativeError("Native context event source was replaced or truncated")
        _check_time(deadline)
    finally:
        os.close(descriptor)


def _event_header(descriptor, size, deadline):
    blocks, start = [], 0
    limit = min(size, _MAX_LINE + 1)
    while start < limit:
        block = _pread_exact(descriptor, start, min(4096, limit - start), deadline)
        newline = block.find(b"\n")
        if newline >= 0:
            blocks.append(block[:newline + 1])
            return b"".join(blocks)
        blocks.append(block)
        start += len(block)
    raise NativeError("Native context event header is incomplete or exceeds its line limit")


def _event_anchor(descriptor, size, deadline):
    blocks, position, remaining = [], size, _MAX_LINE + 2
    while position and remaining:
        length = min(position, remaining, _EVENT_CHUNK)
        start = position - length
        block = _pread_exact(descriptor, start, length, deadline)
        if position == size and not block.endswith(b"\n"):
            raise NativeError("Native context event capture reached an incomplete record")
        end = len(block) - 1 if position == size else len(block)
        newline = block.rfind(b"\n", 0, end)
        if newline >= 0:
            blocks.append(block[newline + 1:])
            return start + newline + 1, b"".join(reversed(blocks))
        blocks.append(block)
        position, remaining = start, remaining - length
        if not position:
            return 0, b"".join(reversed(blocks))
    raise NativeError("Native context event anchor exceeds the supported line limit")


def _prepare_event_receipt(captured, profile, path, deadline):
    with _event_source(path, deadline) as (descriptor, facts, windows):
        first = _event_header(descriptor, facts.st_size, deadline)
        header, ordinal_base = _header(first, captured.identity, profile, deadline)
        anchor_start, anchor = _event_anchor(descriptor, facts.st_size, deadline)
        record = _decode(anchor[:-1], deadline)
        ordinal = record.get("ordinal")
        if type(ordinal) is not int or ordinal < ordinal_base:
            raise NativeError("Native context event anchor has an invalid ordinal")
        _seal_window(windows, 0, first)
        _seal_window(windows, anchor_start, anchor)
        if anchor_start:
            _seal_window(windows, anchor_start - 1, b"\n")
        return {"schema": 2, "kind": _EVENT_KIND, "actor": captured.identity.actor,
                "session_id": captured.identity.session_id, "turn_key": captured.turn_key,
                "tool_id": captured.tool_id, "path": str(path), "device": facts.st_dev,
                "inode": facts.st_ino, "offset": facts.st_size, "header_sha256": header,
                "ordinal_base": ordinal_base, "anchor_start": anchor_start,
                "anchor_sha256": hashlib.sha256(anchor).hexdigest(), "anchor_ordinal": ordinal,
                "profile": profile}


def _event_lines(descriptor, start, end, deadline):
    buffer, position, record_start = bytearray(), start, start
    while position < end:
        block = _pread_exact(descriptor, position, min(end - position, _EVENT_CHUNK), deadline)
        position += len(block)
        buffer.extend(block)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) > _MAX_LINE:
                    raise NativeError("Native context line exceeds the supported limit")
                break
            if newline > _MAX_LINE:
                raise NativeError("Native context line exceeds the supported limit")
            raw = bytes(buffer[:newline + 1])
            del buffer[:newline + 1]
            yield record_start, raw
            record_start += len(raw)


def _verify_event_receipt(identity, request, profile, text, token, deadline):
    if profile["sha256"] not in _STRUCTURAL_EVENT_PROFILES:
        raise NativeError("Unsupported native Codex event receipt writer")
    offset, start = request["offset"], request["anchor_start"]
    ordinal_base, anchor_ordinal = request["ordinal_base"], request["anchor_ordinal"]
    if (type(offset) is not int or not 1 <= offset <= _MAX_EVENT_OFFSET
            or type(start) is not int or not 0 <= start < offset
            or offset - start > _MAX_LINE + 1
            or type(ordinal_base) is not int or ordinal_base < 0
            or type(anchor_ordinal) is not int or anchor_ordinal < ordinal_base):
        raise NativeError("Invalid native context event boundary")
    path = _path(identity, request["path"])
    with _event_source(path, deadline) as (descriptor, facts, windows):
        if (facts.st_dev, facts.st_ino) != (request["device"], request["inode"]) or facts.st_size < offset:
            raise NativeError("Native context event source was replaced or truncated")
        first = _event_header(descriptor, facts.st_size, deadline)
        header, base = _header(first, identity, profile, deadline)
        if header != request["header_sha256"] or base != ordinal_base:
            raise NativeError("Native context event recipient header changed")
        if ((start == 0 and offset != len(first)) or 0 < start < len(first)
                or start and _pread_exact(descriptor, start - 1, 1, deadline) != b"\n"):
            raise NativeError("Native context event anchor is not a complete record boundary")
        anchor = _pread_exact(descriptor, start, offset - start, deadline)
        if not anchor.endswith(b"\n") or hashlib.sha256(anchor).hexdigest() != request["anchor_sha256"]:
            raise NativeError("Native context event anchor changed")
        record = _decode(anchor[:-1], deadline)
        if type(record.get("ordinal")) is not int or record["ordinal"] != anchor_ordinal:
            raise NativeError("Native context event anchor ordinal changed")
        _seal_window(windows, 0, first)
        _seal_window(windows, start, anchor)
        if start:
            _seal_window(windows, start - 1, b"\n")
        ordinal, digest = anchor_ordinal + 1, hashlib.sha256()
        scan_end = min(facts.st_size, offset + _MAX_EVENT_TAIL)
        for position, raw in _event_lines(descriptor, offset, scan_end, deadline):
            record = _decode(raw[:-1], deadline)
            if type(record.get("ordinal")) is not int or record["ordinal"] != ordinal:
                raise NativeError("Native context event ordinal is missing or discontinuous")
            digest.update(raw)
            payload = record.get("payload", {})
            metadata = payload.get("internal_chat_message_metadata_passthrough", {}) if isinstance(payload, dict) else None
            if (record.get("type") == "response_item" and isinstance(payload, dict)
                    and payload.get("type") == "message" and payload.get("role") == "developer"
                    and payload.get("content") == [{"type": "input_text", "text": text}]
                    and isinstance(metadata, dict) and metadata.get("turn_id") == request["turn_key"][6:]
                    and metadata.get("content_item_kinds") == ["hooks.additional_context"]
                    and _hook_provenance(metadata.get("content_item_metadata"))
                    and isinstance(payload.get("id"), str) and 0 < len(payload["id"]) <= 256):
                end = position + len(raw)
                windows.append((offset, end - offset, digest.hexdigest()))
                _check_time(deadline)
                return {"status": "accepted", "evidence": {
                    "source": _EVENT_KIND, "actor": identity.actor,
                    "session_id": identity.session_id, "turn_key": request["turn_key"],
                    "tool_id": request["tool_id"], "token": token,
                    "message_id": payload["id"], "ordinal": ordinal,
                    "byte_start": position, "byte_end": end, "profile": profile}}
            ordinal += 1
        if scan_end < facts.st_size:
            raise NativeError("Native context event tail exceeds the bounded scan limit")
        return {"status": "unknown", "reason": "Exact native hook context has not been recorded"}


def inspect_context_history(identity, profile, transcript_path, *, timeout=0.5):
    """Inspect exact file/header compatibility; do not certify an emitted receipt."""
    observed = {}
    try:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise NativeError("Native context timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        profile = _profile(profile)
        path = _path(identity, transcript_path)
        if profile["sha256"] in _STRUCTURAL_EVENT_PROFILES:
            with _event_source(path, deadline) as (descriptor, facts, windows):
                data = _event_header(descriptor, facts.st_size, deadline)
                _seal_window(windows, 0, data)
        else:
            data, facts = _snapshot(path, deadline)
        newline = data.find(b"\n")
        if newline < 0:
            raise NativeError("Native context header is incomplete")
        record = _decode(data[:newline], deadline)
        header = record.get("payload", {})
        if not isinstance(header, dict):
            raise NativeError("Native context header is malformed")
        observed = {"path": str(path), "size_bytes": facts.st_size,
                    "owner_version": profile["version"], "header_version": header.get("cli_version"),
                    "history_mode": header.get("history_mode")}
        try:
            _header(data, identity, profile, deadline)
        except NativeError as error:
            _check_time(deadline)
            return {"status": "unsupported", "reason": str(error), "evidence": observed}
        _check_time(deadline)
        return {"status": "supported", "reason": "Native creation header is compatible with this recipient and append-owner profile",
                "evidence": observed}
    except (NativeError, OSError, RuntimeError, TypeError) as error:
        return {"status": "unverified", "reason": str(error), "evidence": observed}


def prepare_context_receipt(captured: HookEvent) -> dict:
    """Capture a native lower bound before a tool hook returns any context."""
    try:
        if (not isinstance(captured, HookEvent) or captured.identity.provider != "codex"
                or captured.event != "post_tool" or captured.native_event != "PostToolUse"
                or not captured.turn_key or not captured.turn_key.startswith("codex:")
                or not 6 < len(captured.turn_key) <= 263 or "\0" in captured.turn_key
                or not captured.tool_id):
            raise NativeError("Native context requires an actual Codex PostToolUse event")
        deadline = time.monotonic() + _CAPTURE_TIMEOUT
        profile = _profile(_owner_profile(deadline))
        path = _path(captured.identity, captured.transcript_path)
        if profile["sha256"] in _STRUCTURAL_EVENT_PROFILES:
            return _prepare_event_receipt(captured, profile, path, deadline)
        data, facts = _snapshot(path, deadline)
        header, ordinal_base = _header(data, captured.identity, profile, deadline)
        if not data.endswith(b"\n"):
            raise NativeError("Native context capture reached an incomplete record")
        line_count = 0
        for start, end in _line_spans(data, deadline):
            record = _decode(data[start:end - 1], deadline)
            if type(record.get("ordinal")) is not int or record["ordinal"] != ordinal_base + line_count:
                raise NativeError("Native context ordinal is missing or discontinuous")
            line_count += 1
        result = {"schema": 1, "kind": _KIND, "actor": captured.identity.actor,
                "session_id": captured.identity.session_id, "turn_key": captured.turn_key,
                "tool_id": captured.tool_id, "path": str(path), "device": facts.st_dev,
                "inode": facts.st_ino, "offset": len(data), "line_count": line_count,
                "ordinal_base": ordinal_base,
                "prefix_sha256": hashlib.sha256(data).hexdigest(),
                "header_sha256": header, "profile": profile}
        _check_time(deadline)
        return result
    except (OSError, RuntimeError, TypeError) as exc:
        raise NativeError("Native context capture is unavailable: " + str(exc)) from exc


def verify_context_receipt(identity: NativeIdentity, attempt: dict, *, timeout=0.5) -> dict:
    """Confirm exact appended harness context; unknown never consumes an offer."""
    try:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise NativeError("Native context timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        if not isinstance(identity, NativeIdentity) or identity.provider != "codex":
            raise NativeError("Native context recipient must be Codex")
        request = attempt["receipt_request"]
        event = (isinstance(request, dict) and type(request.get("schema")) is int
                 and request["schema"] == 2 and request.get("kind") == _EVENT_KIND)
        if not event and (not isinstance(request, dict) or request.get("schema") != 1 or request.get("kind") != _KIND):
            raise NativeError("Unsupported native context receipt request")
        if (request["actor"] != identity.actor or request["session_id"] != identity.session_id
                or attempt["actor"] != identity.actor
                or attempt["expected_turn_key"] != request["turn_key"]
                or attempt["tool_id"] != request["tool_id"]):
            raise NativeError("Native context attempt recipient, turn, or tool does not match")
        profile = _profile(request["profile"])
        text = attempt["emission"]["context"]
        token = attempt["token"]
        if parse_wake(text) != token:
            raise NativeError("Native context does not match its exact notification token")
        if event:
            return _verify_event_receipt(identity, request, profile, text, token, deadline)
        offset = request["offset"]
        if type(offset) is not int or offset < 1 or offset > _MAX_FILE:
            raise NativeError("Invalid native context lower bound")
        if type(request["line_count"]) is not int or request["line_count"] < 1:
            raise NativeError("Invalid native context ordinal")
        # Earlier schema-1 captures admitted only zero-based native histories.
        ordinal_base = request.get("ordinal_base", 0)
        if type(ordinal_base) is not int or ordinal_base < 0:
            raise NativeError("Invalid native context ordinal base")
        path = _path(identity, request["path"])
        data, facts = _snapshot(path, deadline)
        if (facts.st_dev, facts.st_ino) != (request["device"], request["inode"]):
            raise NativeError("Native context source was replaced")
        if (len(data) < offset or data[offset - 1] != 10
                or hashlib.sha256(memoryview(data)[:offset]).hexdigest() != request["prefix_sha256"]
                or data.count(b"\n", 0, offset) != request["line_count"]):
            raise NativeError("Native context baseline changed or was truncated")
        header, header_base = _header(data, identity, profile, deadline)
        if header != request["header_sha256"] or header_base != ordinal_base:
            raise NativeError("Native context recipient header changed")
        ordinal = ordinal_base + request["line_count"]
        for position, end in _line_spans(data, deadline, start=offset):
            record = _decode(data[position:end - 1], deadline)
            if type(record.get("ordinal")) is not int or record["ordinal"] != ordinal:
                raise NativeError("Native context ordinal is missing or discontinuous")
            payload = record.get("payload", {})
            if record.get("type") == "response_item" and isinstance(payload, dict):
                metadata = payload.get("internal_chat_message_metadata_passthrough", {})
                provenance = metadata.get("content_item_metadata") if isinstance(metadata, dict) else None
                if (payload.get("type") == "message" and payload.get("role") == "developer"
                        and payload.get("content") == [{"type": "input_text", "text": text}]
                        and isinstance(metadata, dict)
                        and metadata.get("turn_id") == request["turn_key"][6:]
                        and metadata.get("content_item_kinds") == ["hooks.additional_context"]
                        and (_hook_provenance(provenance) if profile["sha256"] not in _LEGACY_PROVENANCE_PROFILES
                             else provenance is None or _hook_provenance(provenance))
                        and isinstance(payload.get("id"), str) and 0 < len(payload["id"]) <= 256):
                    _check_time(deadline)
                    return {"status": "accepted", "evidence": {
                        "source": _KIND, "actor": identity.actor,
                        "session_id": identity.session_id, "turn_key": request["turn_key"],
                        "tool_id": request["tool_id"], "token": token,
                        "message_id": payload["id"], "ordinal": record["ordinal"],
                        "byte_start": position, "byte_end": end,
                        "profile": profile}}
            ordinal += 1
        return {"status": "unknown", "reason": "Exact native hook context has not been recorded"}
    except (NativeError, OSError, RuntimeError, TypeError, KeyError, RecursionError) as exc:
        return {"status": "unknown", "reason": str(exc)}
