"""Claude native identity and socket admission, independent of any backend.

Desktop hosts and CLI engines are distinct native addresses. Only a verified
invoking PID selects a current actor; copied host variables never select one.
CLI creation is a pure observation until a later current registry binds it.
"""
from __future__ import annotations

from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time
import uuid

from . import ancestry, files, lineage, process
from .types import DeliveryResult, IdentityError, NativeError, NativeIdentity

CLI_VERSION = "2.1.310"
CLI_SHA256 = "ffb64331bc0ee1b69efb7c2c428bd3ab516bc31b75bf60886b356c97c1c25ffd"
MAC_CLI_317_VERSION = "2.1.317"
MAC_CLI_317_SHA256 = "09f153b46b5df9e8b3454716d682a91901de020cbca630f61dec1a015b32b5e9"
LINUX_CLI_VERSION = "2.1.311"
LINUX_CLI_SHA256 = "86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada"
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_MIRRORS = 256
MAX_RECIPIENT_RECORDS = 4096


def _platform():
    return sys.platform


def _cli_profile(digest=None):
    if _platform() == "darwin":
        profiles = ((CLI_VERSION, CLI_SHA256), (MAC_CLI_317_VERSION, MAC_CLI_317_SHA256))
    elif _platform() == "linux":
        profiles = ((LINUX_CLI_VERSION, LINUX_CLI_SHA256),)
    else:
        raise IdentityError("Standalone Claude platform is unsupported")
    if digest is None:
        return profiles[0]
    for version, expected in profiles:
        if digest == expected:
            return version, expected
    raise IdentityError("Standalone Claude executable version is unsupported")


def _pid_domain():
    if _platform() == "darwin":
        return "darwin"
    if _platform() != "linux":
        raise IdentityError("Native Claude platform is unsupported")
    try:
        return process.pid_domain()
    except (OSError, ValueError) as exc:
        raise IdentityError("Native Claude PID domain is unresolved") from exc


def _environment(env):
    return os.environ if env is None else env


def _home(env):
    return Path(env.get("HOME") or Path.home()).expanduser().resolve()


def _config(env):
    return Path(env.get("CLAUDE_CONFIG_DIR") or _home(env) / ".claude").expanduser().resolve()


def _uuid(value):
    return lineage.canonical_uuid(value)


def _host(value):
    if not isinstance(value, str) or not value.startswith("local_"):
        raise IdentityError("Expected an exact Claude Desktop host ID")
    _uuid(value[6:])
    return value


def _read_json(path, maximum=MAX_METADATA_BYTES, *, deadline=None):
    """Read one owned non-symlink regular file with bounded consistent bytes."""
    path = Path(path)
    try:
        if deadline is not None:
            files.remaining(deadline)
        if path.resolve(strict=True) != path:
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o022 or not 0 < before.st_size <= maximum):
                raise ValueError
            data = stream.read(maximum + 1)
            after, latest = os.fstat(stream.fileno()), path.lstat()
            signature = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                      item.st_mtime_ns, item.st_ctime_ns)
            if (len(data) != before.st_size or signature(before) != signature(after)
                    or signature(before) != signature(latest)
                    or latest.st_uid != os.getuid() or latest.st_mode & 0o022
                    or not stat.S_ISREG(latest.st_mode)):
                raise ValueError
        result = json.loads(data, object_pairs_hook=lineage._object)
        if not isinstance(result, dict):
            raise ValueError
        if deadline is not None:
            files.remaining(deadline)
        return result
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, NativeError):
            raise IdentityError(str(exc)) from exc
        raise IdentityError("Native Claude metadata is unavailable, unsafe or partially written") from exc


def _invoker(payload, env):
    if payload is not None and (not isinstance(payload, dict) or payload.get("agent_id")):
        raise IdentityError("Claude subagents cannot identify their parent's native session")
    if env.get("CODEX_THREAD_ID"):
        raise IdentityError("Claude and Codex native identity variables conflict")
    engine = payload.get("session_id") if payload is not None else None
    own = env.get("CLAUDE_CODE_SESSION_ID")
    if engine is not None and own and engine != own:
        raise IdentityError("Claude hook and current engine identity disagree")
    engine = _uuid(engine or own)
    raw = env.get("CLAUDE_PID", "")
    if (not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal()
            or len(raw) > 10 or not 1 < int(raw) < 2**31):
        raise IdentityError("Claude current native runtime PID is unavailable")
    return engine, int(raw)


def _plain_arguments(argv, executable, *, deadline=None):
    """Bare local TUI or a narrowly reviewed hooks-only test settings profile."""
    if not argv:
        return False
    first = Path(argv[0])
    if argv[0] != "claude" and (not first.is_absolute() or first.resolve() != executable):
        return False
    if len(argv) == 1:
        return True
    options = {}
    for argument in argv[1:]:
        if argument == "--strict-mcp-config":
            key, value = argument, True
        elif isinstance(argument, str) and "=" in argument:
            key, value = argument.split("=", 1)
            if key not in {"--setting-sources", "--settings", "--mcp-config"}:
                return False
        else:
            return False
        if key in options:
            return False
        options[key] = value
    if options.get("--setting-sources") != "" or "--settings" not in options:
        return False
    try:
        path = Path(options["--settings"])
        if not path.is_absolute():
            return False
        settings = _read_json(path, 1024 * 1024, deadline=deadline)
        if (set(settings) != {"hooks"} or not isinstance(settings["hooks"], dict)
                or set(settings["hooks"]) -
                {"SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "StopFailure", "PostToolUse"}):
            return False
        if "--mcp-config" in options:
            path = Path(options["--mcp-config"])
            if (options.get("--strict-mcp-config") is not True or not path.is_absolute()
                    or _read_json(path, 1024 * 1024, deadline=deadline) != {"mcpServers": {}}):
                return False
        elif "--strict-mcp-config" in options:
            return False
    except (IdentityError, OSError, ValueError, TypeError):
        return False
    return True


def _cli_facts(engine, pid, env, invoking=True, timeout=3):
    deadline = files.deadline(timeout)
    domain = _pid_domain()
    verified = process.invoking(pid, timeout=files.remaining(deadline)) if invoking else None
    if verified is None:
        _, owner, birth = process.identity(pid, files.remaining(deadline))
        if owner != os.getuid():
            raise IdentityError("Claude native process has another owner")
        verified = {"pid": pid, "uid": owner, "proc_start": birth}
    try:
        executable, argv = process.inspect(pid)
        executable = Path(executable).resolve(strict=True)
        digest = files.executable_sha256(executable, deadline)
        version, expected_sha = _cli_profile(digest)
        installed = (_home(env) / ".local/bin/claude").resolve()
        pinned = _home(env) / ".local/share/claude/versions" / version
        managed = (_home(env) / ".local/share/murmur/native-cli/claude" /
                   expected_sha / "package/claude")
        if executable not in {installed, pinned, managed}:
            raise IdentityError("Standalone Claude executable is not the supported native installation")
        # Validate argv[0] before interpreting any option: KERN_PROCARGS2 cannot
        # distinguish empty argv[0] from alignment padding and an env suffix.
        if (not argv or (argv[0] != "claude" and
                (not Path(argv[0]).is_absolute() or Path(argv[0]).resolve() != executable))):
            raise IdentityError("Standalone Claude argv[0] is unsupported")
        root = _plain_arguments(argv, executable, deadline=deadline)
        selector = (argv[2] if len(argv) == 3 and argv[1] == "--resume" else
                    argv[1][len("--resume="):] if len(argv) == 2 and argv[1].startswith("--resume=")
                    else None)
        try:
            resumed = selector is not None and _uuid(selector) == selector
        except IdentityError:
            resumed = False
        if not root and not resumed:
            raise IdentityError("Standalone Claude launch arguments are unsupported")
        _, owner, birth = process.identity(pid, min(files.remaining(deadline), 1))
        if (owner, birth) != (verified["uid"], verified["proc_start"]):
            raise IdentityError("Standalone Claude process epoch changed during inspection")
        if _platform() == "linux" and process.inspect(pid) != (str(executable), argv):
            raise IdentityError("Standalone Claude executable or arguments changed during inspection")
        files.remaining(deadline)
    except (OSError, ValueError) as exc:
        raise IdentityError(f"Standalone Claude process facts unresolved: {exc}") from exc
    platform = {"platform": "linux", "pid_domain": domain} if _platform() == "linux" else {}
    return {**verified, **platform, "entrypoint": "cli", "version": version,
            "root_start_verified": root, "config_dir": str(_config(env)),
            "home_dir": str(_home(env))}


def _epoch(engine, pid, birth, domain="darwin"):
    parts = [engine, pid, birth]
    if domain != "darwin":
        parts.append(domain)
    return json.dumps(parts, separators=(",", ":"))


def _target(record, config, metadata_root=None, cli=None):
    result = {"runtime": "cli" if record.get("hostSessionId") is None else "desktop",
              "engine_id": _uuid(record.get("sessionId")), "pid": record["pid"],
              "proc_start": record["procStart"], "config_dir": str(config),
              "entrypoint": record.get("entrypoint"),
              "native_epoch": _epoch(record["sessionId"], record["pid"], record["procStart"], record["pidDomain"])}
    if record["pidDomain"] != "darwin":
        result.update(platform="linux", pid_domain=record["pidDomain"])
    socket_path = record.get("messagingSocketPath")
    if isinstance(socket_path, str) and len(socket_path) <= 4096:
        result["socket_path"] = socket_path
    if metadata_root is not None:
        result["metadata_root"] = str(metadata_root)
    if cli:
        result.update({key: cli[key] for key in ("root_start_verified", "version", "home_dir")})
    return result


def current_identity(payload=None, *, env=None, timeout=3):
    deadline = files.deadline(timeout)
    env = _environment(env)
    engine, pid = _invoker(payload, env)
    config = _config(env)
    record = _read_json(config / "sessions" / f"{pid}.json", deadline=deadline)
    if (type(record.get("pid")) is not int or record["pid"] != pid
            or record.get("sessionId") != engine or record.get("pidDomain") != _pid_domain()
            or not isinstance(record.get("procStart"), str) or not record["procStart"].strip()):
        raise IdentityError("Claude registry and current native engine or PID disagree")
    process.invoking(pid, record["procStart"], timeout=files.remaining(deadline))
    host = record.get("hostSessionId")
    if host is None:
        if record.get("entrypoint") != "cli" or record.get("kind") != "interactive":
            raise IdentityError("Standalone Claude runtime is unsupported")
        facts = _cli_facts(engine, pid, env, timeout=files.remaining(deadline))
        if record.get("version") != facts["version"]:
            raise IdentityError("Standalone Claude runtime is unsupported")
        return NativeIdentity("claude", engine, target=_target(record, config, cli=facts))
    if (_platform() != "darwin" or host != env.get("CLAUDE_CODE_HOST_SESSION_ID")
            or record.get("entrypoint") not in ("claude-desktop", "claude-desktop-3p")):
        raise IdentityError("Claude Desktop host disagrees with verified invoking runtime")
    root = Path(env.get("MURMUR_CLAUDE_METADATA_ROOT") or
                _home(env) / "Library/Application Support/Claude/claude-code-sessions").expanduser().resolve()
    target = _target(record, config, root)
    evidence = {}
    # Only a verified genuine hook supplies history discovery. Capture bounded
    # lexical facts here; profile hashing and canonical file reads run elsewhere.
    if payload is not None and payload.get("hook_event_name") in {
            "SessionStart", "UserPromptSubmit", "Stop", "StopFailure"}:
        value, version = payload.get("transcript_path"), record.get("version")
        if (isinstance(value, str) and 0 < len(value) <= 4096 and "\0" not in value
                and Path(value).is_absolute() and ".." not in Path(value).parts
                and Path(value).is_relative_to(config / "projects")
                and Path(value).name == engine + ".jsonl"
                and isinstance(version, str) and 0 < len(version) <= 64
                and not any(char in version for char in ("\n", "\r", "\0"))):
            evidence = {"claude_peer_transcript_path": value,
                        "claude_peer_engine_version": version,
                        "claude_peer_capture_epoch": target["native_epoch"]}
    return NativeIdentity("claude", _host(host), target=target, evidence=evidence)


def capture_creation(payload, env=None):
    """Capture standalone creation before the registry flips; no enrollment."""
    if not isinstance(payload, dict):
        raise IdentityError("Claude hook payload must be an object")
    if payload.get("agent_id") or payload.get("hook_event_name") not in {"SessionStart", "SessionEnd"}:
        return None
    env = _environment(env)
    engine, pid = _invoker(payload, env)
    # An exact still-live Desktop record excludes CLI capture. It cannot prove
    # a CLI, and a stale Desktop record from a reused PID does not hide one.
    try:
        record = _read_json(_config(env) / "sessions" / f"{pid}.json")
    except IdentityError:
        record = None
    if (record and record.get("hostSessionId") is not None
            and record.get("entrypoint") in ("claude-desktop", "claude-desktop-3p")):
        caller = process.invoking(pid)
        if caller["proc_start"] == record.get("procStart"):
            return None
    facts = _cli_facts(engine, pid, env)
    return lineage.observation(payload, facts)


def identity_from_creation(observation):
    """Provisional hook address only; never a certified actor or delivery target."""
    if not isinstance(observation, dict):
        raise IdentityError("Claude creation observation must be an object")
    cleaned = lineage.observation(observation.get("payload"), observation.get("process"))
    if cleaned != observation:
        raise IdentityError("Claude creation observation changed")
    facts, payload = observation["process"], observation["payload"]
    engine = payload["session_id"]
    target = {**facts, "engine_id": engine, "runtime": "cli",
              "native_epoch": _epoch(engine, facts["pid"], facts["proc_start"], facts.get("pid_domain", "darwin"))}
    return NativeIdentity("claude", engine, target=target, evidence={"creation": observation})


def _desktop_record(root, host, deadline):
    _host(host)
    records = []
    for path in Path(root).glob(f"*/*/{host}.json"):
        if time.monotonic() >= deadline or len(records) >= MAX_MIRRORS:
            raise IdentityError("Claude Desktop metadata exceeds bounded mirror inspection")
        data = _read_json(path, deadline=deadline)
        if data.get("sessionId") != host:
            raise IdentityError("Claude Desktop record identity disagrees with filename")
        engine = _uuid(data.get("cliSessionId"))
        parent = data.get("forkedFromSessionId")
        spawned = data.get("spawnedFrom")
        fork_marker = ((isinstance(spawned, dict) and spawned.get("userFork") is True)
                       or data.get("forkedAtMessageUuid") is not None)
        if parent is None and fork_marker:
            raise IdentityError("Claude Desktop direct fork parent is unresolved")
        if parent is not None:
            _host(parent)
            if parent == host:
                raise IdentityError("Claude Desktop session cannot be its own parent")
        records.append({"session_id": host, "engine_id": engine, "parent_session_id": parent})
    if not records:
        raise IdentityError("Claude Desktop metadata is not persisted yet")
    if any(item != records[0] for item in records[1:]):
        raise IdentityError("Claude Desktop account mirrors disagree on identity or direct parent")
    return {**records[0], "mirror_count": len(records)}


def resolve_identity(identity, *, timeout=3, lineage_proof=None):
    if identity.provider != "claude" or timeout <= 0:
        raise IdentityError("A Claude native identity and positive timeout are required")
    target = identity.target
    if target.get("runtime") == "desktop":
        deadline = files.deadline(timeout)
        verified = verify_target(identity, timeout=timeout)
        record = _desktop_record(target["metadata_root"], identity.session_id, deadline)
        if record["engine_id"] != target.get("engine_id"):
            raise IdentityError("Claude Desktop metadata engine changed; recapture current identity")
        closure = ancestry.extend("claude", identity.session_id, record["parent_session_id"],
                                  lambda session: _desktop_record(target["metadata_root"], session, deadline)["parent_session_id"])
        return replace(verified, parent_session_id=record["parent_session_id"], parent_known=True,
                       evidence={**identity.evidence, **closure,
                                 "parent_proof": {"kind": "desktop_mirrors", **record}})
    if target.get("runtime") != "cli":
        raise IdentityError("Claude native runtime is unresolved")
    # Creation capture can precede the registry switch; certification cannot.
    verified = verify_target(identity, timeout=timeout)
    supplied = lineage_proof or {}
    if not isinstance(supplied, dict):
        raise IdentityError("Claude CLI requires coordinator-owned creation proof")
    known = supplied.get("known", ())
    if "ancestors" in supplied:
        known = [*known, *supplied["ancestors"]]
    proof = lineage.certify(identity.session_id, observations=supplied.get("observations", ()),
                            known=known, config_dir=target["config_dir"], process={
                                **verified.target, "entrypoint": "cli"})
    return replace(verified, parent_session_id=proof["parent_session_id"], parent_known=True,
                   evidence={**identity.evidence, "parent_proof": proof, "lineage_complete": True,
                             "lineage_edges": [{"session_id": item["session_id"],
                                                "parent_session_id": item["parent_session_id"]}
                                               for item in reversed(proof["ancestors"])]})


def _fresh_record(identity, timeout):
    deadline = files.deadline(timeout)
    target = identity.target
    config_value = target.get("config_dir")
    if not isinstance(config_value, str) or not Path(config_value).is_absolute():
        raise IdentityError("Claude target lacks its verified native config routing")
    config = Path(config_value)
    pid = target.get("pid")
    if type(pid) is not int or not 1 < pid < 2**31:
        raise IdentityError("Claude target lacks a native PID")
    record = _read_json(config / "sessions" / f"{pid}.json", deadline=deadline)
    engine, birth = target.get("engine_id"), target.get("proc_start")
    domain = _pid_domain()
    if (type(record.get("pid")) is not int or record["pid"] != pid
            or record.get("pidDomain") != domain or record.get("sessionId") != engine
            or not isinstance(birth, str) or not birth or record.get("procStart") != birth
            or target.get("native_epoch") != _epoch(engine, pid, birth, domain)
            or (domain != "darwin" and (target.get("platform") != "linux" or target.get("pid_domain") != domain))):
        raise IdentityError("Claude target native epoch changed; recapture identity")
    _, owner, current_birth = process.identity(pid, files.remaining(deadline))
    if owner != os.getuid() or current_birth != birth:
        raise IdentityError("Claude target process ownership or birth changed")
    if identity.session_id.startswith("local_"):
        if (_platform() != "darwin" or record.get("hostSessionId") != _host(identity.session_id)
                or record.get("entrypoint") not in ("claude-desktop", "claude-desktop-3p")):
            raise IdentityError("Claude target Desktop host binding changed")
        facts = None
    else:
        if (record.get("hostSessionId") is not None or record.get("sessionId") != _uuid(identity.session_id)
                or record.get("entrypoint") != "cli" or record.get("kind") != "interactive"):
            raise IdentityError("Claude target standalone registry binding changed")
        # Only routing HOME is needed here; executable/version checks are local.
        home = target.get("home_dir")
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise IdentityError("Claude CLI target lacks its verified executable home routing")
        facts = _cli_facts(engine, pid, {"HOME": home, "CLAUDE_CONFIG_DIR": str(config)},
                           invoking=False, timeout=files.remaining(deadline))
        if (record.get("version") != facts["version"]
                or target.get("version", facts["version"]) != facts["version"]):
            raise IdentityError("Claude target standalone registry binding changed")
    fresh = _target(record, config, target.get("metadata_root"), cli=facts)
    return record, replace(identity, target=fresh)


def verify_target(identity, timeout=3):
    if identity.provider != "claude" or timeout <= 0:
        raise IdentityError("Expected a Claude native target")
    return _fresh_record(identity, timeout)[1]


def owner_process_state(identity, *, timeout=3):
    """Inspect only the recorded owner epoch; never infer exit from a new engine.

    `ended` requires positive PID absence or a different observed process birth.
    Inaccessible or malformed evidence remains `unknown`. This helper has no
    registry, lifecycle, messaging or persistence effects.
    """
    import math
    import subprocess

    recorded = {}

    def result(status, reason, observed=None):
        value = {"status": status, "reason": reason, "recorded": recorded}
        if observed is not None:
            value["observed"] = observed
        return value

    try:
        if (not isinstance(identity, NativeIdentity) or identity.provider != "claude"
                or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError
        target = identity.target
        pid, birth, engine = target.get("pid"), target.get("proc_start"), target.get("engine_id")
        if (type(pid) is not int or not 1 < pid < 2**31
                or not isinstance(birth, str) or not birth.strip() or len(birth) > 256
                or any(char in birth for char in ("\n", "\r", "\0"))):
            raise ValueError
        _uuid(engine)
        domain = target.get("pid_domain", "darwin")
        if (domain != _pid_domain() or target.get("native_epoch") != _epoch(engine, pid, birth, domain)):
            raise ValueError
        desktop = identity.session_id.startswith("local_")
        if (target.get("runtime") != ("desktop" if desktop else "cli")
                or (not desktop and identity.session_id != engine)):
            raise ValueError
        recorded = {"pid": pid, "proc_start": birth, "engine_id": engine,
                    "native_epoch": target["native_epoch"]}
    except (ValueError, TypeError, AttributeError):
        return result("unknown", "invalid_recorded_target")

    deadline = time.monotonic() + timeout
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return result("ended", "pid_missing")
        return result("unknown", "pid_probe_denied" if exc.errno in {errno.EPERM, errno.EACCES}
                      else "pid_probe_failed")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return result("unknown", "inspection_timed_out")
    try:
        parent, owner, observed_birth = process.identity(pid, remaining)
        if (type(parent) is not int or parent < 0 or type(owner) is not int
                or not 0 <= owner < 2**32 or not isinstance(observed_birth, str)
                or not observed_birth.strip() or len(observed_birth) > 256
                or any(char in observed_birth for char in ("\n", "\r", "\0"))):
            raise ValueError
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # The process can exit between kill(0) and ps. One final absence check
        # can prove that race; success/permission failure still proves nothing.
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return result("ended", "pid_missing_after_inspection")
        return result("unknown", "inspection_unavailable")
    observed = {"uid": owner, "proc_start": observed_birth}
    if observed_birth != birth:
        return result("ended", "pid_reused", observed)
    if owner != os.getuid():
        return result("unknown", "owner_changed", observed)
    return result("alive", "same_process", observed)


def _unique_recipient(identity, deadline, *, strict=False):
    """Reject a second live process claiming the same exact native address.

    New idle preflight also holds on unreadable or unaddressable rows; it has
    not yet frozen an attempt that would need to retain its original route.
    """
    sessions = Path(identity.target["config_dir"]) / "sessions"
    count = 0
    for path in sessions.glob("*.json"):
        count += 1
        if count > MAX_RECIPIENT_RECORDS or time.monotonic() >= deadline:
            raise IdentityError("Claude recipient registry exceeds bounded freshness inspection")
        if path.name == f"{identity.target['pid']}.json":
            continue
        try:
            record = _read_json(path, deadline=deadline)
        except IdentityError:
            if strict:
                raise
            continue
        if strict:
            _registry_address(record, identity, path)
        if identity.session_id.startswith("local_"):
            matches = (record.get("hostSessionId") == identity.session_id and
                       (strict or record.get("entrypoint") in ("claude-desktop", "claude-desktop-3p")))
        else:
            matches = (record.get("hostSessionId") is None and
                       record.get("sessionId") == identity.session_id and
                       (strict or record.get("entrypoint") == "cli"))
        if not matches:
            continue
        pid = record.get("pid")
        if (type(pid) is not int or not 1 < pid < 2**31 or path.name != f"{pid}.json"
                or record.get("pidDomain") != _pid_domain() or not isinstance(record.get("procStart"), str)):
            raise IdentityError("Claude has ambiguous matching native recipient records")
        try:
            # An exited PID is positive stale-record evidence. A permission
            # failure cannot certify uniqueness and remains a visible blocker.
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise IdentityError("Claude matching recipient process cannot be inspected") from exc
        _, owner, birth = process.identity(pid, max(0.001, deadline-time.monotonic()))
        if birth != record["procStart"]:
            continue
        if owner != os.getuid():
            raise IdentityError("Claude matching recipient process has another owner")
        raise IdentityError("Claude native recipient is ambiguous across live processes")


def _socket_address(path):
    """Expand only macOS's verified, root-controlled /tmp system alias."""
    if sys.platform != "darwin" or path.parts[:2] != ("/", "tmp"):
        return path
    alias, canonical = Path("/tmp"), Path("/private/tmp")
    try:
        link, root = alias.lstat(), canonical.lstat()
        if (not stat.S_ISLNK(link.st_mode) or link.st_uid != 0
                or os.readlink(alias) not in ("private/tmp", "/private/tmp")
                or canonical.resolve(strict=True) != canonical
                or not stat.S_ISDIR(root.st_mode) or root.st_uid != 0
                or not root.st_mode & stat.S_ISVTX):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise IdentityError("Claude native /tmp alias is not the trusted macOS temporary directory") from exc
    return canonical / path.relative_to(alias)


def _socket(record):
    value = record.get("messagingSocketPath")
    if not isinstance(value, str) or len(value) > 4096 or not Path(value).is_absolute():
        raise IdentityError("Claude native messaging socket is unavailable")
    path = _socket_address(Path(value))
    try:
        info, parent = path.lstat(), path.parent.lstat()
        if (path.parent.resolve(strict=True) != path.parent or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
            raise ValueError
    except (OSError, ValueError) as exc:
        raise IdentityError("Claude native messaging socket is unsafe or unavailable") from exc
    return path


def _idle_registry(identity):
    value = identity.target.get("config_dir")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise IdentityError("Claude target lacks its verified native config routing")
    config = Path(value)
    sessions = config / "sessions"
    for path in (config, sessions):
        info = path.lstat()
        if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise IdentityError("Claude recipient registry directory is unsafe")
    return config, sessions


def _registry_address(record, identity, path):
    engine, host = _uuid(record.get("sessionId")), record.get("hostSessionId")
    if host is not None:
        _host(host)
    elif record.get("entrypoint") == "claude-vscode" and engine != identity.target.get("engine_id"):
        # Observed VS Code registries omit a Desktop host. A complete row for
        # another engine is outside this exact same-engine recovery scope; it
        # does not qualify VS Code as a recipient or excuse a partial row.
        pid, birth, endpoint = record.get("pid"), record.get("procStart"), record.get("messagingSocketPath")
        if (type(pid) is not int or not 1 < pid < 2**31 or path.name != f"{pid}.json"
                or not isinstance(birth, str) or not birth.strip() or len(birth) > 256
                or any(char in birth for char in ("\n", "\r", "\0"))
                or record.get("pidDomain") != identity.target.get("pid_domain", "darwin")
                or record.get("kind") != "interactive" or record.get("status") not in {"idle", "busy"}
                or not isinstance(record.get("version"), str) or not record["version"]
                or not isinstance(endpoint, str) or len(endpoint) > 4096 or not Path(endpoint).is_absolute()):
            raise IdentityError("Claude unbound registry row is incomplete")
    elif record.get("entrypoint") != "cli":
        raise IdentityError("Claude registry row has no complete native recipient address")
    return engine, host


def _recover_idle_record(identity, deadline):
    """Find a unique same-engine replacement after the caller proved exit."""
    if not identity.parent_known:
        raise IdentityError("Claude idle recovery requires a previously certified identity")
    target = identity.target
    config, sessions = _idle_registry(identity)
    engine, domain = _uuid(target.get("engine_id")), _pid_domain()
    desktop = identity.session_id.startswith("local_")
    live = []
    for count, path in enumerate(sessions.glob("*.json"), 1):
        if count > MAX_RECIPIENT_RECORDS:
            raise IdentityError("Claude recipient registry exceeds bounded recovery inspection")
        files.remaining(deadline)
        # An unreadable row cannot prove that a second matching recipient is
        # absent. Hold recovery until the registry is safely readable again.
        record = _read_json(path, deadline=deadline)
        record_engine, host = _registry_address(record, identity, path)
        matches = (host == identity.session_id if desktop else
                   host is None and record_engine == identity.session_id)
        if not matches:
            continue
        pid, birth = record.get("pid"), record.get("procStart")
        if (type(pid) is not int or not 1 < pid < 2**31 or path.name != f"{pid}.json"
                or record.get("pidDomain") != domain or not isinstance(birth, str)
                or not birth.strip() or len(birth) > 256
                or any(char in birth for char in ("\n", "\r", "\0"))):
            raise IdentityError("Claude has malformed matching native recipient records")
        fresh = _target(record, config, target.get("metadata_root"))
        if not desktop:
            fresh["home_dir"] = target.get("home_dir")
        candidate = replace(identity, target=fresh)
        state = owner_process_state(candidate, timeout=files.remaining(deadline))
        if state["status"] == "ended":
            continue
        if state["status"] != "alive":
            raise IdentityError("Claude matching recipient process is unresolved: " + state["reason"])
        if fresh["engine_id"] != engine:
            raise IdentityError("Claude Desktop engine changed; capture a fresh native identity")
        live.append(candidate)
        if len(live) > 1:
            raise IdentityError("Claude native recipient is ambiguous across live processes")
    if not live:
        raise IdentityError("Claude native recipient is not loaded with a verified live owner")
    record, current = _fresh_record(live[0], files.remaining(deadline))
    if desktop:
        root = target.get("metadata_root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise IdentityError("Claude Desktop target lacks verified metadata routing")
        mirror = _desktop_record(root, identity.session_id, deadline)
        if (mirror["engine_id"] != engine or mirror["parent_session_id"] != identity.parent_session_id):
            raise IdentityError("Claude Desktop recovery metadata changed its engine or direct parent")
    return record, current


def _ready_idle_target(record, identity, deadline):
    if record.get("status") != "idle":
        raise IdentityError("Claude target has no explicit live idle status")
    _idle_registry(identity)
    _unique_recipient(identity, deadline, strict=True)
    endpoint = _socket(record)
    latest, current = _fresh_record(identity, files.remaining(deadline))
    if latest.get("status") != "idle" or _socket(latest) != endpoint:
        raise IdentityError("Claude native target changed before idle preparation completed")
    files.remaining(deadline)
    return current


def recover_idle_target(identity, *, timeout=3):
    """Rediscover only a fresh idle route; never rebind an existing attempt.

    The coordinator must separately fence enrollment, active/open work and the
    old stored snapshot. A changed engine requires a fresh human hook. An old
    registry row disappearing does not establish that its owner ended.
    """
    try:
        deadline = files.deadline(timeout)
        if not isinstance(identity, NativeIdentity) or identity.provider != "claude":
            raise IdentityError("Expected a Claude native target")
        state = owner_process_state(identity, timeout=files.remaining(deadline))
        if state["status"] != "ended":
            raise IdentityError("Claude recorded owner has not verifiably ended: " + state["reason"])
        record, current = _recover_idle_record(identity, deadline)
        return _ready_idle_target(record, current, deadline)
    except IdentityError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise IdentityError("Claude idle recovery is unavailable: " + str(exc)) from exc


def prepare_idle_target(identity, *, timeout=3):
    """Verify idleness before a new claim, optionally recovering an ended PID."""
    try:
        deadline = files.deadline(timeout)
        if not isinstance(identity, NativeIdentity) or identity.provider != "claude":
            raise IdentityError("Expected a Claude native target")
        try:
            record, current = _fresh_record(identity, files.remaining(deadline))
        except (OSError, ValueError) as exc:
            state = owner_process_state(identity, timeout=files.remaining(deadline))
            if state["status"] != "ended":
                raise IdentityError("Claude idle target is unresolved; recorded owner "
                                    + state["reason"] + ": " + str(exc)) from exc
            record, current = _recover_idle_record(identity, deadline)
        return _ready_idle_target(record, current, deadline)
    except IdentityError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise IdentityError("Claude idle preparation is unavailable: " + str(exc)) from exc


def deliver(identity, text, token, timeout=5):
    wrote, bound, reply_path, evidence = False, False, None, None
    try:
        if (not isinstance(text, str) or not text or not isinstance(token, str) or not token
                or len(token) > 1024 or timeout <= 0):
            raise IdentityError("Claude delivery requires text, token and a positive timeout")
        deadline = time.monotonic() + timeout
        record, current = _fresh_record(identity, timeout)
        _unique_recipient(current, deadline)
        if record.get("status") != "idle":
            raise IdentityError("Claude target has no explicit live idle status")
        target = _socket(record)
        evidence = current.target
        reply_path = target.parent / f"{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"murmur:{token}"))
        frame = {"msgV": 1, "msg_id": message_id, "type": "user", "priority": "next",
                 "from": f"uds:{reply_path}", "message": {"role": "user", "content": text}}
        if current.target["runtime"] == "cli":
            frame["session_id"] = current.target["engine_id"]
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(reply_path))
            bound = True
            os.chmod(reply_path, 0o600)
            listener.listen(4)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(max(0.001, deadline - time.monotonic()))
                client.connect(str(target))
                # Recheck the exact epoch after connecting, before any bytes.
                latest, _ = _fresh_record(identity, max(0.001, deadline - time.monotonic()))
                if _socket(latest) != target or latest.get("status") != "idle":
                    raise IdentityError("Claude native target changed before socket submission")
                wrote = True
                client.sendall(json.dumps(frame).encode() + b"\n")
                client.shutdown(socket.SHUT_WR)
                while (remaining := deadline - time.monotonic()) > 0:
                    listener.settimeout(remaining)
                    connection, _ = listener.accept()
                    with connection:
                        pending = b""
                        while len(pending) < 65536 and (remaining := deadline - time.monotonic()) > 0:
                            connection.settimeout(remaining)
                            block = connection.recv(min(4096, 65536 - len(pending)))
                            if not block:
                                break
                            pending += block
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                try:
                                    receipt = json.loads(line)
                                except (ValueError, RecursionError):
                                    continue
                                if (not isinstance(receipt, dict) or receipt.get("type") != "control"
                                        or receipt.get("action") != "peer_message_status"
                                        or receipt.get("orig_msg_id") != message_id):
                                    continue
                                status = receipt.get("status")
                                if status == "delivered":
                                    return DeliveryResult("accepted", "Claude native delivery receipt; model admission unconfirmed", target=evidence)
                                if status == "held":
                                    return DeliveryResult("uncertain", "Claude retained input under inbound controls", target=evidence)
                                if status in {"denied", "expired", "refused", "dropped"}:
                                    return DeliveryResult("rejected", f"Claude reported {status}", target=evidence)
        return DeliveryResult("uncertain", "Claude write completed without a matching receipt", target=evidence)
    except (OSError, ValueError) as exc:
        return DeliveryResult("uncertain" if wrote else "rejected",
                              f"Claude native admission {'unconfirmed' if wrote else 'unavailable'}: {exc}",
                              target=evidence if wrote else None)
    finally:
        if bound and reply_path is not None:
            try:
                reply_path.unlink(missing_ok=True)
            except OSError:
                pass  # Cleanup failure must not rewrite the admission outcome.
