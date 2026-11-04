"""Bounded receipts for the measured Linux Claude CLI PostToolUse format.

Hook stdout is an offer. Only a newly persisted native context attachment with
the captured tool/result/prompt ancestry confirms that offer's insertion.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from . import claude
from .hooks import parse_wake
from .types import HookEvent, NativeError, NativeIdentity

_KIND = "claude-cli-hook-context"
_VERSION = "2.1.311"
_SHA256 = "86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada"
_MAX_FILE = 32 * 1024 * 1024
_MAX_LINE = 2 * 1024 * 1024
_CONVERSATION = {"user", "assistant", "attachment", "system", "progress"}


def _profile(value=None):
    expected = {"platform": "linux", "version": _VERSION, "sha256": _SHA256}
    if value is not None and value != expected:
        raise NativeError("Unsupported native Claude context profile")
    return expected


def _recipient(identity):
    if (not isinstance(identity, NativeIdentity) or identity.provider != "claude"
            or identity.target.get("platform") != "linux"
            or identity.target.get("version") != _VERSION
            or identity.target.get("runtime") != "cli"
            or identity.target.get("entrypoint") != "cli" or not identity.epoch):
        raise NativeError("Native Claude context requires the pinned Linux CLI recipient")


def _invoking(identity, deadline):
    _recipient(identity)
    if claude._platform() != "linux" or claude._cli_profile() != (_VERSION, _SHA256):
        raise NativeError("Invoking Claude build is not a certified context profile")
    from .files import remaining
    actual = claude.current_identity(timeout=remaining(deadline))
    if actual.actor != identity.actor or actual.target != identity.target:
        raise NativeError("Native Claude context requires its actual invoking recipient")
    return _profile()


def _deadline(timeout):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise NativeError("Native context timeout must be finite and positive")
    return time.monotonic() + timeout


def _check(deadline):
    if time.monotonic() >= deadline:
        raise NativeError("Native Claude context inspection exceeded its deadline")


def _path(identity, value):
    if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
        raise NativeError("Native context requires an absolute transcript path")
    root = Path(identity.target["config_dir"]) / "projects"
    path = Path(value)
    if (root.resolve(strict=True) != root or path.resolve(strict=True) != path
            or not path.is_relative_to(root) or path.name != identity.session_id + ".jsonl"):
        raise NativeError("Native Claude context lies outside its canonical session store")
    return path


def _snapshot(path, deadline):
    _check(deadline)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or not 0 < before.st_size <= _MAX_FILE):
            raise NativeError("Native context file ownership, mode or size is unsupported")
        data = stream.read(_MAX_FILE + 1)
        after = os.fstat(stream.fileno())
        latest = path.lstat()
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                  item.st_mtime_ns, item.st_ctime_ns)
        if (len(data) != before.st_size or signature(before) != signature(after)
                or signature(before) != signature(latest) or not stat.S_ISREG(latest.st_mode)
                or latest.st_uid != os.getuid() or latest.st_mode & 0o077
                or path.resolve(strict=True) != path):
            raise NativeError("Native context file changed during inspection")
    _check(deadline)
    return data, before


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeError("Duplicate native context JSON field")
        result[key] = value
    return result


def _constant(value):
    raise NativeError("Non-JSON native context constant: " + value)


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise NativeError("Native context UUID is malformed")
    return value


def _records(data, deadline):
    records, index, offset = [], {}, 0
    for line in data.splitlines(keepends=True):
        _check(deadline)
        if not line.endswith(b"\n"):
            break
        if not 0 < len(line) <= _MAX_LINE:
            raise NativeError("Native context line exceeds its bound")
        try:
            value = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
        except RecursionError as error:
            raise NativeError("Native context JSON exceeds its depth bound") from error
        if not isinstance(value, dict):
            raise NativeError("Native context record must be an object")
        # Keep validation workspace proportional to depth, not record width.
        pending, visited = [(iter((value,)), 0)], 0
        while pending:
            children, depth = pending[-1]
            try:
                item = next(children)
            except StopIteration:
                pending.pop()
                continue
            visited += 1
            if visited % 1024 == 0:
                _check(deadline)
            if depth > 128:
                raise NativeError("Native context JSON exceeds its depth bound")
            if isinstance(item, dict):
                pending.append((iter(item.values()), depth + 1))
            elif isinstance(item, list):
                pending.append((iter(item), depth + 1))
        records.append((offset, value))
        if value.get("type") in _CONVERSATION:
            key = _uuid(value.get("uuid"))
            if key in index:
                raise NativeError("Native context UUID is duplicated")
            index[key] = value
        offset += len(line)
    return records, index


def _node(value, identity):
    if (value.get("type") not in _CONVERSATION or value.get("isSidechain") is not False
            or value.get("sessionId") != identity.session_id
            or value.get("session_id", identity.session_id) != identity.session_id
            or value.get("version") != _VERSION):
        raise NativeError("Native context has another recipient, version or sidechain")
    _uuid(value.get("uuid"))
    if value.get("parentUuid") is not None:
        _uuid(value["parentUuid"])


def _prompt(index, start, identity, prompt_id):
    seen = set()
    for _ in range(4096):
        _node(start, identity)
        key = start["uuid"]
        if key in seen:
            raise NativeError("Native context ancestry contains a cycle")
        seen.add(key)
        if start.get("type") == "user":
            blocks = start.get("message", {}).get("content", [])
            result = isinstance(blocks, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks)
            if start.get("promptId", prompt_id) != prompt_id:
                raise NativeError("Native tool belongs to another prompt")
            if not result:
                if (start.get("promptId") != prompt_id
                        or not isinstance(start.get("promptSource"), str) or not start["promptSource"]
                        or start.get("message", {}).get("role") != "user"):
                    raise NativeError("Native original prompt metadata is unavailable")
                _uuid(prompt_id)
                return key
        start = index.get(start.get("parentUuid"), {})
    raise NativeError("Native context prompt ancestry exceeds its bound")


def _tool(index, identity, prompt_id, tool_id):
    matches = []
    for node in index.values():
        if node.get("type") != "assistant":
            continue
        blocks = node.get("message", {}).get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_id:
                matches.append((node, block))
    if len(matches) != 1:
        raise NativeError("Native context tool is missing or ambiguous")
    node, block = matches[0]
    if not isinstance(block.get("name"), str) or not block["name"]:
        raise NativeError("Native context tool name is unavailable")
    return node["uuid"], _prompt(index, node, identity, prompt_id), block["name"]


def _prepare_context_receipt(captured: HookEvent):
    deadline = _deadline(.5)
    if (not isinstance(captured, HookEvent) or captured.event != "post_tool"
            or captured.native_event != "PostToolUse" or not captured.tool_id
            or not captured.turn_key):
        raise NativeError("Native Claude context requires an actual PostToolUse event")
    profile = _invoking(captured.identity, deadline)
    _check(deadline)
    prefix = "claude:" + captured.identity.session_id + ":"
    if not captured.turn_key.startswith(prefix):
        raise NativeError("Native Claude prompt key does not match its recipient")
    prompt_id = _uuid(captured.turn_key[len(prefix):])
    path = _path(captured.identity, captured.transcript_path)
    data, facts = _snapshot(path, deadline)
    if not data.endswith(b"\n"):
        raise NativeError("Native context capture reached an incomplete record")
    _, index = _records(data, deadline)
    tool, prompt, name = _tool(index, captured.identity, prompt_id, captured.tool_id)
    _check(deadline)
    return {"schema": 1, "kind": _KIND, "actor": captured.identity.actor,
            "session_id": captured.identity.session_id, "epoch": captured.identity.epoch,
            "turn_key": captured.turn_key, "prompt_id": prompt_id, "tool_id": captured.tool_id,
            "tool_uuid": tool, "prompt_uuid": prompt, "tool_name": name, "path": str(path),
            "device": facts.st_dev, "inode": facts.st_ino, "offset": len(data),
            "prefix_sha256": hashlib.sha256(data).hexdigest(), "profile": profile}


def prepare_context_receipt(captured: HookEvent):
    try:
        return _prepare_context_receipt(captured)
    except NativeError:
        raise
    except (ValueError, OSError, RuntimeError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise NativeError("Native Claude context capture is unavailable: " + str(error)) from error


def _context_ancestry(node, index, identity, request):
    result_seen, seen = False, set()
    for _ in range(64):
        _node(node, identity)
        if node["uuid"] in seen:
            raise NativeError("Native context ancestry contains a cycle")
        seen.add(node["uuid"])
        if node["uuid"] == request["tool_uuid"]:
            if not result_seen:
                raise NativeError("Native context lacks its exact tool result")
            return
        if node["type"] == "user":
            blocks = node.get("message", {}).get("content", [])
            if (node.get("promptId", request["prompt_id"]) != request["prompt_id"]
                    or not isinstance(blocks, list) or "promptSource" in node
                    or node.get("message", {}).get("role") != "user"):
                raise NativeError("Native context crossed another user prompt")
            result_seen = any(isinstance(b, dict) and b.get("type") == "tool_result"
                              and b.get("tool_use_id") == request["tool_id"]
                              and b.get("is_error") is not True for b in blocks)
            if not result_seen:
                raise NativeError("Native context has another tool result")
        elif node["type"] not in {"attachment"}:
            raise NativeError("Native context is detached from its captured tool")
        node = index.get(node.get("parentUuid"), {})
    raise NativeError("Native context tool ancestry exceeds its bound")


def verify_context_receipt(identity, attempt, *, timeout=.5):
    try:
        deadline = _deadline(timeout)
        _recipient(identity)
        request = attempt["receipt_request"]
        if request.get("schema") != 1 or request.get("kind") != _KIND:
            raise NativeError("Unsupported native Claude receipt request")
        _profile(request["profile"])
        if (request["actor"] != identity.actor or attempt["actor"] != identity.actor
                or request["session_id"] != identity.session_id or request["epoch"] != identity.epoch
                or request["turn_key"] != attempt["expected_turn_key"]
                or request["turn_key"] != "claude:" + identity.session_id + ":" + request["prompt_id"]
                or request["tool_id"] != attempt["tool_id"]):
            raise NativeError("Native context recipient, owner, prompt or tool changed")
        text = attempt["emission"]["context"]
        if parse_wake(text) != attempt["token"]:
            raise NativeError("Native context notification token does not match")
        path = _path(identity, request["path"])
        data, facts = _snapshot(path, deadline)
        offset = request["offset"]
        if (type(offset) is not int or not 0 < offset <= len(data)
                or not data[:offset].endswith(b"\n")
                or (facts.st_dev, facts.st_ino) != (request["device"], request["inode"])
                or hashlib.sha256(data[:offset]).hexdigest() != request["prefix_sha256"]):
            raise NativeError("Native context baseline was replaced, changed or truncated")
        _, baseline = _records(data[:offset], deadline)
        binding = _tool(baseline, identity, request["prompt_id"], request["tool_id"])
        if binding != (request["tool_uuid"], request["prompt_uuid"], request["tool_name"]):
            raise NativeError("Native context captured prompt/tool binding changed")
        records, index = _records(data, deadline)
        for position, node in records:
            attachment = node.get("attachment", {})
            if (position >= offset and node.get("type") == "attachment"
                    and attachment.get("type") == "hook_additional_context"
                    and attachment.get("content") == [text]
                    and attachment.get("hookEvent") == "PostToolUse"
                    and attachment.get("toolUseID") == request["tool_id"]
                    and attachment.get("hookName") == "PostToolUse:" + request["tool_name"]):
                _context_ancestry(node, index, identity, request)
                rendered = "<system-reminder>\n" + attachment["hookName"] + " hook additional context: " + text + "\n</system-reminder>"
                if node.get("renderedRole") != "system" or node.get("rendered") != [{"content": rendered}]:
                    raise NativeError("Native context rendering does not match its exact attachment")
                _check(deadline)
                return {"status": "accepted", "evidence": {"source": _KIND, "actor": identity.actor,
                        "session_id": identity.session_id, "turn_key": request["turn_key"],
                        "tool_id": request["tool_id"], "token": attempt["token"],
                        "message_id": node["uuid"], "byte_start": position, "profile": request["profile"]}}
        return {"status": "unknown", "reason": "Exact native Claude hook context has not been recorded"}
    except (ValueError, OSError, RuntimeError, TypeError, KeyError, AttributeError, RecursionError) as error:
        return {"status": "unknown", "reason": str(error)}


def inspect_context_history(identity, transcript_path=None, *, timeout=.5):
    """Inspect this invoking recipient's pinned profile and native history only."""
    try:
        deadline = _deadline(timeout)
        _invoking(identity, deadline)
        _check(deadline)
        if transcript_path is None:
            return {"status": "unverified", "reason": "No trusted native Claude transcript path was supplied"}
        data, _ = _snapshot(_path(identity, transcript_path), deadline)
        _, index = _records(data, deadline)
        if not index:
            raise NativeError("Native Claude history has no recipient records")
        for node in index.values():
            _node(node, identity)
        return {"status": "supported", "reason": "Pinned Linux Claude CLI profile and native history verified; admission is separate"}
    except (ValueError, OSError, RuntimeError, TypeError, KeyError, RecursionError) as error:
        return {"status": "unverified", "reason": str(error)}
