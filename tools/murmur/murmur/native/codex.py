"""Exact Codex identity, read-only ancestry and one-shot queue delivery.

The queue producer uses the captured cwd and native configuration environment.
Qualified Desktop recipients also receive a wake-only app request after enqueue.
Unknown startup overrides are never recovered by guessing. Transport acceptance
never proves that a model turn began.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
import uuid

from .hooks import validate_doorbell
from . import ancestry, desktop_wake
from .types import DeliveryResult, IdentityError, NativeError, NativeIdentity

_BUNDLED = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
_ROUTE_ENV = ("HOME", "CODEX_HOME", "CODEX_SQLITE_HOME")
_CLAUDE_ENV = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID")
_MAX_OUTPUT = 1024 * 1024
_MAX_LINE = 256 * 1024


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise IdentityError("Codex requires an exact canonical native thread UUID") from exc
    return value


def _environment(env):
    result = dict(os.environ if env is None else env)
    if any(result.get(key) for key in _CLAUDE_ENV):
        raise IdentityError("Codex identity has conflicting Claude runtime variables")
    return result


def _string(value, name):
    if not isinstance(value, str) or not value or len(value) > 8192 or "\0" in value:
        raise IdentityError(f"Invalid Codex {name}")
    return value


def _target(env, payload=None, executable=None):
    selected = executable or env.get("MURMUR_CODEX_BINARY")
    if selected:
        selected = _string(selected, "executable")
        if not Path(selected).is_absolute():
            raise IdentityError("Codex executable override must be absolute")
    else:
        selected = _BUNDLED if Path(_BUNDLED).is_file() else shutil.which("codex", path=env.get("PATH", os.defpath))
    directory = (payload or {}).get("cwd", os.getcwd())
    directory = _string(directory, "working directory")
    if not Path(directory).is_absolute():
        raise IdentityError("Codex working directory must be absolute")
    route_env = {key: env.get(key) for key in _ROUTE_ENV}
    route_env["HOME"] = route_env["HOME"] or str(Path.home())
    for key, value in route_env.items():
        if value is not None:
            _string(value, key)
    return {"binary": str(Path(selected).resolve()) if selected else None,
            "cwd": str(Path(directory).resolve()), "env": route_env,
            "cli_args": [], "transport": "queue"}


def current_identity(payload=None, *, env=None) -> NativeIdentity:
    """Read this invoking session; hooks do no native RPC or provisioning."""
    values = _environment(env)
    if payload is not None and not isinstance(payload, dict):
        raise IdentityError("Codex hook payload must be an object")
    hooked = (payload or {}).get("session_id")
    own = values.get("CODEX_THREAD_ID")
    if hooked is not None:
        _uuid(hooked)
    if own is not None:
        _uuid(own)
    if hooked is not None and own is not None and hooked != own:
        raise IdentityError("Codex hook and current thread identity disagree")
    session_id = _uuid(hooked if hooked is not None else own)
    evidence = {"identity_source": "hook" if hooked is not None else "environment",
                "routing_scope": "captured cwd/env; startup CLI overrides unobserved"}
    # Explicit absence clears an earlier Desktop capability if this same native
    # conversation is subsequently resumed by a CLI client.
    evidence["desktop_wake"] = desktop_wake.capture(values)
    return NativeIdentity("codex", session_id, parent_known=False,
                          target=_target(values, payload), evidence=evidence)


def _route(identity, *, env=None, executable=None):
    if not isinstance(identity, NativeIdentity) or identity.provider != "codex":
        raise IdentityError("Expected a Codex native identity")
    _uuid(identity.session_id)
    target = dict(identity.target)
    if not target:
        target = _target(_environment(env), executable=executable)
    elif executable is not None or env is not None:
        raise IdentityError("Captured Codex route cannot be replaced; resolve a fresh identity")
    if target.get("transport") != "queue":
        raise IdentityError("Unsupported Codex native transport")
    binary = _string(target.get("binary"), "executable")
    directory = _string(target.get("cwd"), "working directory")
    if not Path(binary).is_absolute() or not Path(directory).is_absolute():
        raise IdentityError("Codex captured executable and cwd must be absolute")
    overrides = target.get("env")
    if not isinstance(overrides, dict) or set(overrides) != set(_ROUTE_ENV):
        raise IdentityError("Codex route lacks captured native configuration environment")
    process_env = dict(os.environ)
    for key, value in overrides.items():
        if value is None:
            process_env.pop(key, None)
        else:
            process_env[key] = _string(value, key)
    args = target.get("cli_args", [])
    if not isinstance(args, list) or len(args) > 64 or len(args) % 2:
        raise IdentityError("Invalid Codex configuration arguments")
    for key, value in zip(args[::2], args[1::2]):
        if not isinstance(key, str) or key not in {"--profile", "-p", "--config", "-c"}:
            raise IdentityError("Only explicit Codex profile/config arguments are supported")
        _string(value, "configuration argument")
        if value.startswith("-"):
            raise IdentityError("Codex configuration values cannot begin with an option prefix")
    return target, [binary, *args], directory, process_env


def _verified_target(identity, timeout):
    _timeout(timeout)
    reason = automatic_enrollment_reason(identity)
    if reason:
        raise IdentityError(reason)
    target, command, directory, _ = _route(identity)
    binary = Path(command[0])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise IdentityError("Codex executable is unavailable or not executable")
    if not Path(directory).is_dir():
        raise IdentityError("Codex captured working directory is unavailable")
    desktop = (desktop_wake.verify(identity, timeout=timeout)
               if identity.evidence.get("desktop_wake") is not None else None)
    return (identity if target == identity.target else replace(identity, target=target)), desktop


def verify_target(identity, timeout=3) -> NativeIdentity:
    """Validate the captured queue route and any explicit Desktop wake evidence."""
    return _verified_target(identity, timeout)[0]


def _timeout(timeout):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise IdentityError("Codex timeout must be finite and positive")
    return float(timeout)


def _stop(process):
    """Reap the short-lived producer, including one that ignores terminate."""
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _read_only_request(identity, method, params, timeout=3, *, env=None, executable=None):
    """Bounded metadata RPC for this exact thread or captured working directory."""
    target, command, directory, process_env = _route(identity, env=env, executable=executable)
    if method == "thread/read":
        request_params = {"threadId": identity.session_id, "includeTurns": False}
        valid = (isinstance(params, dict) and set(params) == set(request_params)
                 and params.get("threadId") == identity.session_id and params.get("includeTurns") is False)
    elif method == "hooks/list":
        request_params = {"cwds": [directory]}
        valid = isinstance(params, dict) and params == request_params
    else:
        raise IdentityError("Codex read-only RPC method is not supported")
    if not valid:
        raise IdentityError("Codex read-only RPC must use its exact captured recipient or cwd")
    deadline = time.monotonic() + _timeout(timeout)
    process = subprocess.Popen([*command, "app-server", "--stdio"], cwd=directory,
                               env=process_env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        def send(value):
            process.stdin.write(json.dumps(value).encode() + b"\n")
            process.stdin.flush()

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "murmur-mattermost-native", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        expected, total, pending = 1, 0, b""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while (remaining := deadline - time.monotonic()) > 0:
                if not selector.select(remaining):
                    break
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    raise IdentityError("Codex app-server closed before returning metadata")
                total += len(block)
                if total > _MAX_OUTPUT:
                    raise IdentityError("Codex metadata exceeds total output bound")
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) > _MAX_LINE:
                        raise IdentityError("Codex metadata exceeds line bound")
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise IdentityError("Codex app-server returned malformed JSON") from exc
                    if not isinstance(value, dict):
                        raise IdentityError("Codex app-server returned an invalid RPC object")
                    if "id" not in value and isinstance(value.get("method"), str):
                        continue
                    if type(value.get("id")) is not int or value["id"] != expected:
                        raise IdentityError("Codex app-server returned an unexpected RPC response")
                    if "error" in value or not isinstance(value.get("result"), dict):
                        raise IdentityError("Codex app-server metadata RPC failed")
                    if expected == 2:
                        return value["result"], target
                    expected = 2
                    send({"method": "initialized"})
                    send({"id": 2, "method": method, "params": request_params})
                if len(pending) > _MAX_LINE:
                    raise IdentityError("Codex metadata exceeds line bound")
        raise IdentityError("Codex metadata lookup timed out")
    finally:
        if not _stop(process):
            raise IdentityError("Codex metadata producer could not be reaped within the cleanup bound")


def _read_thread(identity, timeout, env, executable):
    return _read_only_request(identity, "thread/read", {"threadId": identity.session_id, "includeTurns": False},
                              timeout, env=env, executable=executable)


def _metadata(identity, *, timeout=3, env=None, executable=None) -> NativeIdentity:
    """Read exact direct-parent metadata without loading or resuming a thread."""
    try:
        result, target = _read_thread(identity, timeout, env, executable)
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != identity.session_id:
            raise IdentityError("Codex app-server returned different or invalid thread metadata")
        if "forkedFromId" not in thread:
            raise IdentityError("Codex parent metadata is unresolved")
        parent = thread["forkedFromId"]
        if parent is not None:
            _uuid(parent)
            if parent == identity.session_id:
                raise IdentityError("Codex metadata contains a self-parent")
        return replace(identity, parent_session_id=parent, parent_known=True, target=target,
                       evidence={**identity.evidence, "parent_source": "app-server:thread/read",
                                 "codex_thread_source": thread.get("source"),
                                 "codex_thread_source_session_id": identity.session_id,
                                 "codex_thread_source_origin": "app-server:thread/read"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityError(f"Codex read-only metadata unavailable: {type(exc).__name__}") from exc


def verified_thread_source(identity):
    """Return only source metadata captured by this exact thread/read lookup."""
    if (not isinstance(identity, NativeIdentity) or identity.provider != "codex"
            or identity.evidence.get("codex_thread_source_origin") != "app-server:thread/read"
            or identity.evidence.get("codex_thread_source_session_id") != identity.session_id):
        return None
    return identity.evidence.get("codex_thread_source")


def automatic_enrollment_reason(identity):
    """Known spawned subagents lack a qualified unloaded direct-input route.

    The .166 Thread API uses subAgent; its state-database serialization uses
    subagent instead. Parent/fork metadata and names do not establish source.
    """
    source = verified_thread_source(identity)
    if not isinstance(source, dict) or set(source) != {"subAgent"}:
        return None
    subagent = source["subAgent"]
    if not isinstance(subagent, dict) or set(subagent) != {"thread_spawn"}:
        return None
    spawned = subagent["thread_spawn"]
    if (not isinstance(spawned, dict) or type(spawned.get("depth")) is not int
            or not -(2**31) <= spawned["depth"] < 2**31):
        return None
    try:
        _uuid(spawned.get("parent_thread_id"))
    except IdentityError:
        return None
    return ("Spawned Codex subagents do not support reliable automatic Murmur delivery after unloading; "
            "only manual messaging is supported")


def resolve_identity(identity, *, timeout=3, env=None, executable=None) -> NativeIdentity:
    """Resolve exact bounded ancestry; unavailable ancestors stay incomplete."""
    deadline = time.monotonic() + _timeout(timeout)
    current = _metadata(identity, timeout=timeout, env=env, executable=executable)

    def parent(session):
        remaining = deadline - time.monotonic()
        ancestor = replace(current, session_id=session, parent_session_id=None, parent_known=False)
        return _metadata(ancestor, timeout=remaining).parent_session_id

    closure = ancestry.extend("codex", current.session_id, current.parent_session_id, parent)
    return replace(current, evidence={**current.evidence, **closure})


def metadata(identity_or_session_id, *, timeout=3, env=None, executable=None):
    """Resolve an exact ancestor using the selected route, never select aliases."""
    identity = identity_or_session_id
    if isinstance(identity, str):
        identity = NativeIdentity("codex", _uuid(identity))
    return resolve_identity(identity, timeout=timeout, env=env, executable=executable)


def deliver(identity, text, token, *, timeout=5) -> DeliveryResult:
    """One queue attempt; any post-spawn ambiguity must not trigger a replay."""
    try:
        timeout = _timeout(timeout)
        if not isinstance(text, str) or "\0" in text:
            raise IdentityError("Codex doorbell must be text without NUL bytes")
        validate_doorbell(text, token)
        identity, desktop = _verified_target(identity, timeout)
        target, command, directory, process_env = _route(identity)
        process = subprocess.Popen([*command, "queue", "--thread", identity.session_id,
                                    "--message", text], cwd=directory, env=process_env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
    except (NativeError, OSError, ValueError) as exc:
        return DeliveryResult("rejected", str(exc))
    try:
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return DeliveryResult("uncertain", "Codex queue completion is unknown; do not automatically replay", target)
        if code:
            return DeliveryResult("uncertain", f"Codex queue exited {code}; acceptance is unknown", target)
        if desktop is not None:
            # The attempt was journaled before entering this one-shot transport.
            # Once queue input may exist, even a wake-only failure is uncertain:
            # retiring it or falling back could enqueue the doorbell twice.
            try:
                wake = desktop_wake.wake(identity.session_id, desktop, timeout=timeout)
                return DeliveryResult("accepted" if wake.status == "accepted" else "uncertain",
                                      "Native queue accepted input; " + wake.detail,
                                      {**target, "desktop_wake": wake.target})
            except (NativeError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                return DeliveryResult("uncertain", "Native queue accepted input; Desktop wake is unconfirmed: "
                                      + str(exc), target)
        return DeliveryResult("accepted", "Native queue accepted input; model admission is unconfirmed", target)
    finally:
        _stop(process)
