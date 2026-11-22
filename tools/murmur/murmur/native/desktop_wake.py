"""Wake an already queued local Desktop thread through its own deep-link route.

Capture uses inherited discovery plus bounded process/file facts, never app RPC.
Verification belongs outside hooks. The wake URL contains no prompt and cannot
select a remote host or another native configuration home. Scheduling that URL
does not prove that Desktop resumed the thread or admitted a model turn.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import socket
import stat
import struct
import subprocess
import sys

from . import files, process
from .types import DeliveryResult, NativeError, NativeIdentity, validate_session_id


_PIPE_ENV = "CODEX_APP_TOOLS_PIPE_PATH"
_TOOLS_DIRECTORY = "/tmp/codex-app-tools"
_KIND = "codex-desktop-deep-link-v1"
_OWNER_RELATIVE_PATH = "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
_CONFIG_OVERRIDES = {
    "features.code_mode_host=true",
    "plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true",
    "plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=false",
    'plugins."codex-app-tools@openai-bundled".mcp_servers.codex_app.enabled=true',
    'plugins."codex-app-tools@openai-bundled".mcp_servers.codex_app.enabled=false',
}


def owner_process_state(identity, *, timeout=3):
    """Inspect the frozen Desktop generation, never its possible successor.

    PID absence or a different positively observed birth proves that component
    ended. An inaccessible inspection, changed UID or malformed descriptor does
    not. This only releases an exact old active turn; it never settles input.
    """
    try:
        until = files.deadline(timeout)
        if (sys.platform != "darwin" or not isinstance(identity, NativeIdentity)
                or identity.provider != "codex"):
            raise NativeError("No captured local Desktop owner")
        value = _identity_descriptor(identity)
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "unknown", "reason": "invalid_recorded_desktop_owner"}
    states = {}
    for name in (("app", "helper", "owner") if "helper" in value else ("app", "owner")):
        recorded = value[name]
        pid, birth = recorded["pid"], recorded["birth"]
        if pid >= 2**31 or len(birth) > 256 or any(c in birth for c in "\n\r\0"):
            return {"status": "unknown", "reason": "invalid_recorded_desktop_owner"}
        try:
            os.kill(pid, 0)
        except OSError as error:
            states[name] = {"status": "ended" if error.errno == errno.ESRCH else "unknown",
                            "reason": "pid_missing" if error.errno == errno.ESRCH else "pid_probe_unavailable"}
            continue
        try:
            parent, uid, current = process.identity(pid, timeout=files.remaining(until))
            if (type(parent) is not int or parent < 0 or type(uid) is not int or uid < 0
                    or not isinstance(current, str) or not current.strip() or len(current) > 256
                    or any(c in current for c in "\n\r\0")):
                raise NativeError("Process inspection is malformed")
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            try:
                os.kill(pid, 0)
            except OSError as error:
                if error.errno == errno.ESRCH:
                    states[name] = {"status": "ended", "reason": "pid_missing_after_inspection"}
                    continue
            states[name] = {"status": "unknown", "reason": "inspection_unavailable"}
            continue
        if current != birth:
            states[name] = {"status": "ended", "reason": "pid_reused"}
        elif uid != recorded["uid"]:
            states[name] = {"status": "unknown", "reason": "owner_changed"}
        else:
            states[name] = {"status": "alive", "reason": "same_process"}
    statuses = {item["status"] for item in states.values()}
    status = ("ended" if states["owner"]["status"] == "ended"
              else "alive" if statuses == {"alive"} else "unknown")
    return {"status": status, "reason": "captured_desktop_generation", "components": states}


def _listing(command, until):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            timeout=files.remaining(until), check=True)
    if len(result.stdout) > 1024 * 1024:
        raise NativeError("Desktop successor discovery output exceeds its bound")
    files.remaining(until)
    return result.stdout.splitlines()


def _database_files(owner, domain, until, *, require_history_handle=True):
    """Pin safe database paths and any open handles; successor checks stay strict."""
    root = Path(domain["codex_home"])
    paths = [root / name for name in ("queue_1.sqlite", "state_5.sqlite", "thread_history_1.sqlite")]
    records, current = [], None
    for line in _listing(["/usr/sbin/lsof", "-nP", "-a", "-p", str(owner["pid"]), "-FfDin"], until):
        if line.startswith('f'):
            current = {}
            records.append(current)
        elif current is not None and line[:1] in {'D', 'i', 'n'}:
            current[line[0]] = line[1:]
    result = {}
    for path in paths:
        facts = path.lstat()
        descriptors = [record for record in records if record.get('n') == str(path)]
        needs_handle = require_history_handle or path.name != "thread_history_1.sqlite"
        if ((needs_handle and not descriptors) or path.resolve(strict=True) != path
                or not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.getuid() or facts.st_mode & 0o022):
            raise NativeError("Desktop successor does not own the safe default native databases: " + path.name)
        if any((int(record.get('D', '-1'), 0), int(record.get('i', '-1'), 10)) != (facts.st_dev, facts.st_ino)
               for record in descriptors):
            raise NativeError("Desktop successor native database FD differs from its pathname: " + path.name)
        result[str(path)] = [facts.st_dev, facts.st_ino, facts.st_uid, facts.st_mode]
    return result


def discover_successor(identity, *, timeout=3):
    """Discover one actual local Desktop generation; never impersonate a caller.

    Read-only process/FD inspection is followed by the ordinary full Desktop
    verifier. Unsupported or multiple app/backend/socket candidates stay held.
    This does not start a process, load a thread, submit text or prove admission.
    """
    try:
        until = files.deadline(timeout)
        if sys.platform != "darwin" or not isinstance(identity, NativeIdentity) or identity.provider != "codex":
            raise NativeError("Desktop successor discovery requires a captured Codex Desktop")
        original = _identity_descriptor(identity)
        bundle = _absolute(original["app_bundle"])
        expected = {"app": {str(bundle / "Contents/MacOS/ChatGPT")},
                    "owner": {str(bundle / _OWNER_RELATIVE_PATH)},
                    "helper": {str(path.resolve(strict=True)) for path in bundle.glob(
                        "Contents/Frameworks/Codex Framework.framework/Versions/*/Helpers/"
                        "Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)")}}
        candidates = {kind: [] for kind in expected}
        rows = _listing(["/bin/ps", "-axo", "pid=,uid=,comm="], until)
        if len(rows) > 16_384:
            raise NativeError("Desktop successor process inventory exceeds its bound")
        for row in rows:
            parts = row.split(None, 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                raise NativeError("Desktop successor process inventory is malformed")
            if int(parts[1]) != os.getuid():
                continue
            for kind, executables in expected.items():
                if parts[2] in executables:
                    candidates[kind].append(int(parts[0]))
        if len(candidates['app']) != 1:
            raise NativeError("Desktop successor app is absent or ambiguous")
        app, app_argv = _inspect(candidates["app"][0], until)
        eligible = []
        for pid in candidates['owner']:
            owner, argv = _inspect(pid, until)
            helper = None
            if owner['parent_pid'] != app['pid']:
                if owner['parent_pid'] not in candidates['helper']:
                    continue
                helper, helper_argv = _inspect(owner['parent_pid'], until)
                if helper['parent_pid'] != app['pid'] or not _helper_role_supported(helper_argv):
                    continue
            if (owner['executable'] in expected['owner']
                    and _startup_supported(argv, app_argv, bundle=bundle, helper=helper)):
                eligible.append((owner, helper))
        if app['executable'] not in expected['app'] or len(eligible) != 1:
            raise NativeError("Desktop successor eligible backend/startup domain is absent or ambiguous")
        owner, helper = eligible[0]
        pipe_root = Path(_TOOLS_DIRECTORY).resolve(strict=True)
        sockets = []
        for line in _listing(["/usr/sbin/lsof", "-nP", "-a", "-p", str(app["pid"]), "-U", "-Fn"], until):
            if not line.startswith("n/"):
                continue
            path = Path(line[1:])
            if path.parent.resolve() != pipe_root or path.suffix != ".sock":
                continue
            validate_session_id("codex", path.stem)
            candidate = _socket(path)
            if candidate not in sockets:
                sockets.append(candidate)
        if len(sockets) != 1:
            raise NativeError("Desktop successor owned app-tools socket is absent or ambiguous")
        value = {"schema": 1, "kind": _KIND, "app_bundle": str(bundle),
                 "app": app, "owner": owner, "pipe": sockets[0],
                 "domain": deepcopy(original["domain"]), "startup_supported": True}
        if helper is not None:
            value["helper"] = helper
        databases = _database_files(owner, value["domain"], until)
        candidate = replace(identity, evidence={**identity.evidence, "desktop_wake": value})
        verified = verify(candidate, timeout=files.remaining(until))
        if _database_files(owner, value["domain"], until) != databases:
            raise NativeError("Desktop successor native database generation changed")
        verified["database_files"] = databases
        return replace(candidate, evidence={**candidate.evidence, "desktop_wake": verified})
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError) as error:
        raise NativeError("Desktop successor discovery is unavailable: " + str(error)) from error


def _absolute(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value or not Path(value).is_absolute():
        raise NativeError("Desktop wake requires a bounded absolute native path")
    return Path(value).resolve()


def _domain(env):
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    home = _absolute(env.get("HOME") or str(real_home))
    codex_home = _absolute(env.get("CODEX_HOME") or str(home / ".codex"))
    sqlite = env.get("CODEX_SQLITE_HOME")
    user_data = env.get("CODEX_ELECTRON_USER_DATA_PATH")
    return {"home": str(home), "codex_home": str(codex_home),
            "sqlite_home": str(_absolute(sqlite)) if sqlite else None,
            "user_data_path": str(_absolute(user_data)) if user_data else None,
            "supported": home == real_home and codex_home == real_home / ".codex"
                         and not sqlite and not user_data}


def _argv_hash(argv):
    return hashlib.sha256(json.dumps(list(argv), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _inspect(pid, until):
    before = process.identity(pid, timeout=min(.2, files.remaining(until)))
    if before[1] != os.getuid():
        raise NativeError("Desktop wake process belongs to another OS user")
    executable, argv = process.inspect(pid)
    if not argv or Path(argv[0]).name != Path(executable).name:
        raise NativeError("Desktop wake process arguments are ambiguous")
    if before != process.identity(pid, timeout=min(.2, files.remaining(until))):
        raise NativeError("Desktop wake process changed during inspection")
    files.remaining(until)
    return {"pid": pid, "parent_pid": before[0], "uid": before[1], "birth": before[2],
            "executable": executable, "argv_sha256": _argv_hash(argv)}, argv


def _socket(path):
    path = Path(path)
    if len(os.fsencode(path)) > 4096 or not path.is_absolute() or path.is_symlink():
        raise NativeError("Desktop wake discovery socket is unsafe")
    path = path.resolve(strict=True)
    info, parent = path.lstat(), path.parent.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
        raise NativeError("Desktop wake discovery socket ownership or mode is unsafe")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def _peer_owner(value, until):
    """Kernel-bind the app-tools address, sending no application bytes.

    Darwin sys/un.h defines SOL_LOCAL=0 and LOCAL_PEERPID=2. Socket FD names in
    lsof cannot prove this: an unlinked/replaced address can retain the old name.
    """
    before, _ = _inspect(value['app']['pid'], until)
    if before != value['app'] or _socket(value['pipe']['path']) != value['pipe']:
        raise NativeError('Desktop app-tools peer generation changed before connection')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(min(.2, files.remaining(until)))
        client.connect(value['pipe']['path'])
        peer = client.getsockopt(0, 2, struct.calcsize('i'))
        if len(peer) != struct.calcsize('i') or struct.unpack('i', peer)[0] != value['app']['pid']:
            raise NativeError('Desktop app-tools socket belongs to another process')
    after, _ = _inspect(value['app']['pid'], until)
    if after != before or _socket(value['pipe']['path']) != value['pipe']:
        raise NativeError('Desktop app-tools peer or socket changed during connection')
    files.remaining(until)


def _helper_relative_path(bundle, helper):
    path = _absolute(helper["executable"])
    relative = path.relative_to(bundle)
    parts = relative.parts
    if (len(parts) != 10 or parts[:4] != ("Contents", "Frameworks", "Codex Framework.framework", "Versions")
            or parts[5:] != ("Helpers", "Codex (Aperitif).app", "Contents", "MacOS", "Codex (Aperitif)")):
        raise NativeError("Desktop wake helper is outside its captured app framework")
    return str(relative)


def _helper_role_supported(argv):
    roles = {"--type": "utility", "--utility-sub-type": "node.mojom.NodeService",
             "--service-sandbox-type": "none"}
    return all([arg for arg in argv[1:] if arg == flag or arg.startswith(flag + "=")]
               == [flag + "=" + value] for flag, value in roles.items())


def _startup_supported(argv, app_argv, *, bundle=None, helper=None):
    if any(arg == "--user-data-dir" or arg.startswith("--user-data-dir=") for arg in app_argv[1:]):
        return False
    args, index, server = list(argv[1:]), 0, 0
    if helper is not None:
        _helper_relative_path(bundle, helper)
        home = str(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".codex")
        node = str(bundle / "Contents/Resources/cua_node/bin/node_repl")
        suffix = ["--plugin-mcp-launch-cwd", "unified-computer-use@openai-bundled", "cua_repl", home,
                  "--mcp-launch-cwd", "node_repl", node, home,
                  "--mcp-launch-cwd", "artifact_session", node, home]
        if args[-len(suffix):] == suffix:
            args = args[:-len(suffix)]
    while index < len(args):
        value = args[index]
        if value in {"-c", "--config"} and index + 1 < len(args):
            if args[index + 1] not in _CONFIG_OVERRIDES:
                return False
            index += 2
        elif value == "app-server":
            server += 1
            index += 1
        elif value == "--analytics-default-enabled":
            index += 1
        else:
            return False
    return server == 1


def _capture(env, timeout):
    values = dict(os.environ if env is None else env)
    if sys.platform != "darwin" or not values.get(_PIPE_ENV):
        return None
    until = files.deadline(timeout)
    pid, seen = os.getppid(), set()
    for _ in range(16):
        if pid <= 1:
            return None
        if pid in seen:
            raise NativeError("Desktop wake invoking ancestry is cyclic")
        seen.add(pid)
        owner, argv = _inspect(pid, until)
        if Path(owner["executable"]).name != "codex":
            pid = owner["parent_pid"]
            continue
        # Never skip a nearer CLI/daemon in favor of a Desktop ancestor.
        if "app-server" not in argv[1:]:
            return None
        app, app_argv = _inspect(owner["parent_pid"], until)
        helper, helper_argv = None, ()
        if Path(app["executable"]).name == "Codex (Aperitif)":
            helper, helper_argv = app, app_argv
            app, app_argv = _inspect(helper["parent_pid"], until)
        executable = Path(app["executable"])
        if (executable.name != "ChatGPT" or executable.parent.name != "MacOS"
                or executable.parent.parent.name != "Contents"
                or executable.parent.parent.parent.suffix != ".app"):
            return None
        bundle = executable.parent.parent.parent
        if Path(owner["executable"]) != bundle / _OWNER_RELATIVE_PATH:
            return None
        if helper is not None:
            _helper_relative_path(bundle, helper)
            if len({app["pid"], helper["pid"], owner["pid"]}) != 3:
                raise NativeError("Desktop wake process chain repeats a PID")
        result = {"schema": 1, "kind": _KIND, "app_bundle": str(bundle),
                  "app": app, "owner": owner, "pipe": _socket(values[_PIPE_ENV]),
                  "domain": _domain(values),
                  "startup_supported": (helper is None or _helper_role_supported(helper_argv))
                                       and _startup_supported(argv, app_argv, bundle=bundle, helper=helper)}
        if helper is not None:
            result["helper"] = helper
        files.remaining(until)
        return result
    raise NativeError("Desktop wake invoking ancestry exceeds its supported bound")


def capture(env=None, *, timeout=.5):
    """Capture optional current Desktop evidence without changing a queue target.

    No discovery hint retains legacy queue behavior without claiming cold wake
    support. A hint with unavailable process proof is an explicit error, never
    permission to impersonate the app or silently certify an inherited CLI.
    """
    try:
        return _capture(env, timeout)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        raise NativeError("Desktop wake capture is unavailable: " + str(error)) from error


def _descriptor(value):
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("kind") != _KIND
            or not isinstance(value.get("domain"), dict) or value["domain"].get("supported") is not True
            or value.get("startup_supported") is not True):
        raise NativeError("Desktop wake requires a supported captured local Desktop domain")
    bundle = _absolute(value["app_bundle"])
    names = ("app", "helper", "owner") if "helper" in value else ("app", "owner")
    for key in names:
        facts = value[key]
        if (not isinstance(facts, dict) or type(facts.get("pid")) is not int or facts["pid"] <= 1
                or facts.get("uid") != os.getuid() or not isinstance(facts.get("birth"), str) or not facts["birth"]
                or not isinstance(facts.get("argv_sha256"), str) or len(facts["argv_sha256"]) != 64
                or type(facts.get("parent_pid")) is not int or facts["parent_pid"] < 0
                or facts["parent_pid"] == facts["pid"]):
            raise NativeError("Desktop wake process capture is invalid")
        _absolute(facts["executable"])
    if len({value[name]["pid"] for name in names}) != len(names):
        raise NativeError("Desktop wake process chain repeats a PID")
    if "helper" in value:
        _helper_relative_path(bundle, value["helper"])
        if (value["helper"]["parent_pid"] != value["app"]["pid"]
                or value["owner"]["parent_pid"] != value["helper"]["pid"]):
            raise NativeError("Desktop wake owner is not the captured app's helper child")
    elif value["owner"]["parent_pid"] != value["app"]["pid"]:
        raise NativeError("Desktop wake owner is not the captured app's direct child")
    return value


def _identity_descriptor(identity):
    """Check frozen routing shape without assuming the old process is alive."""
    if not isinstance(identity, NativeIdentity) or identity.provider != "codex":
        raise NativeError("Desktop wake requires a Codex recipient")
    value = _descriptor(identity.evidence.get("desktop_wake"))
    target = identity.target
    if (target.get("transport") != "queue" or target.get("cli_args") != []
            or not isinstance(target.get("env"), dict)
            or set(target["env"]) != {"HOME", "CODEX_HOME", "CODEX_SQLITE_HOME"}
            or _domain(target["env"]) != value["domain"]):
        raise NativeError("Desktop wake cannot target another native configuration domain")
    bundle = _absolute(value["app_bundle"])
    if (Path(value["app"]["executable"]) != bundle / "Contents/MacOS/ChatGPT"
            or Path(value["owner"]["executable"]) != bundle / _OWNER_RELATIVE_PATH):
        raise NativeError("Desktop wake executable disagrees with its captured bundle")
    producer = _absolute(target.get("binary"))
    if (producer not in {(bundle / "Contents/Resources/codex-cli/bin/codex").resolve(),
                         (bundle / _OWNER_RELATIVE_PATH).resolve()}
            or not isinstance(target.get("cwd"), str) or not Path(target["cwd"]).is_absolute()):
        raise NativeError("Desktop wake queue producer is outside the captured app route")
    return value


def _recheck(value, until, *, peer=False):
    app, app_argv = _inspect(value["app"]["pid"], until)
    owner, argv = _inspect(value["owner"]["pid"], until)
    helper = None
    if "helper" in value:
        helper, helper_argv = _inspect(value["helper"]["pid"], until)
        if helper != value["helper"] or not _helper_role_supported(helper_argv):
            raise NativeError("Desktop wake helper generation changed")
    if (app != value["app"] or owner != value["owner"]
            or not _startup_supported(argv, app_argv, bundle=_absolute(value["app_bundle"]), helper=helper)
            or _socket(value["pipe"]["path"]) != value["pipe"]):
        raise NativeError("Desktop wake app, backend or socket generation changed")
    if peer:
        _peer_owner(value, until)


def _signature(path):
    facts = path.lstat()
    if (not stat.S_ISREG(facts.st_mode) or facts.st_uid not in {0, os.getuid()} or facts.st_mode & 0o022):
        raise NativeError("Desktop wake application file is unsafe")
    return [facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns]


def _profile_topology(bundle, profile, helper):
    if ("helper_relative_path" in profile) != (helper is not None):
        raise NativeError("Desktop wake process topology disagrees with its observed app profile")
    if helper is not None and _helper_relative_path(bundle, helper) != profile["helper_relative_path"]:
        raise NativeError("Desktop wake helper disagrees with its observed app profile")


def _profile(bundle, owner, producer, until, *, helper=None):
    info_path = bundle / "Contents/Info.plist"
    before = _signature(info_path)
    with info_path.open("rb") as source:
        data = source.read(256 * 1024 + 1)
    if len(data) > 256 * 1024 or _signature(info_path) != before:
        raise NativeError("Desktop wake app metadata changed or exceeds its bound")
    info = plistlib.loads(data)
    if (info.get("CFBundleIdentifier") != "com.openai.codex" or info.get("CFBundleExecutable") != "ChatGPT"
            or any(not isinstance(info.get(key), str) or not 0 < len(info[key]) <= 256
                   or any(char in info[key] for char in "\n\r\0")
                   for key in ("CFBundleShortVersionString", "CFBundleVersion"))):
        raise NativeError("Desktop wake application identity or metadata is invalid")
    profile = {"bundle_id": info["CFBundleIdentifier"], "version": info["CFBundleShortVersionString"],
               "build": info["CFBundleVersion"]}
    if helper is not None:
        profile.update(helper_relative_path=_helper_relative_path(bundle, helper), launch_cwd_args=True)
    paths = [(bundle / "Contents/MacOS/ChatGPT", "app_sha256"),
             (bundle / "Contents/Resources/app.asar", "asar_sha256"),
             (bundle / _OWNER_RELATIVE_PATH, "owner_sha256")]
    if Path(owner["executable"]) != paths[2][0]:
        raise NativeError("Desktop wake backend is outside the captured app")
    launcher = bundle / "Contents/Resources/codex-cli/bin/codex"
    if producer not in {paths[2][0], launcher}:
        raise NativeError("Desktop wake producer is outside the captured app")
    paths.append((launcher, "launcher_sha256"))
    if helper is not None:
        paths.append((bundle / profile["helper_relative_path"], "helper_sha256"))
    signatures = {str(info_path): before}
    for path, key in paths:
        initial = _signature(path)
        profile[key] = files.executable_sha256(path, until)
        if _signature(path) != initial:
            raise NativeError("Desktop wake application source changed during inspection")
        signatures[str(path)] = initial
    return profile, signatures


def verify(identity: NativeIdentity, *, timeout=3):
    """Verify a frozen current owner/domain outside hooks, with no app RPC."""
    try:
        until = files.deadline(timeout)
        value = _identity_descriptor(identity)
        target = identity.target
        bundle = _absolute(value["app_bundle"])
        producer = _absolute(target.get("binary"))
        _recheck(value, until)
        profile, signatures = _profile(bundle, value["owner"], producer, until, helper=value.get("helper"))
        listing = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(value["app"]["pid"]), "-U", "-Fn"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=files.remaining(until), check=True)
        if len(listing.stdout) > 1024 * 1024:
            raise NativeError("Desktop wake socket ownership output exceeds its bound")
        names = (row[1:] for row in listing.stdout.splitlines() if row.startswith("n/"))
        if not any(str(Path(name).resolve()) == value["pipe"]["path"] for name in names):
            raise NativeError("Captured Desktop app does not own its discovery socket")
        _recheck(value, until, peer=True)
        files.remaining(until)
        result = deepcopy(value)
        result["verification"] = {"profile": dict(profile), "files": signatures}
        return result
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError) as error:
        raise NativeError("Desktop wake preflight is unavailable: " + str(error)) from error


def wake(session_id, descriptor, *, timeout=3):
    """Schedule one background wake URL; callers must never requeue its text."""
    try:
        until = files.deadline(timeout)
        validate_session_id("codex", session_id)
        value = _descriptor(descriptor)
        proof = value.get("verification", {})
        if not isinstance(proof.get("profile"), dict) or not proof.get("files"):
            raise NativeError("Desktop wake requires an outside-hook verified app profile")
        bundle = _absolute(value["app_bundle"])
        profile, signatures = _profile(bundle, value["owner"], bundle / _OWNER_RELATIVE_PATH, until,
                                       helper=value.get("helper"))
        if profile != proof["profile"] or signatures != proof["files"]:
            raise NativeError("Desktop wake observed application proof changed")
        _profile_topology(_absolute(value["app_bundle"]), proof["profile"], value.get("helper"))
        if "helper" in value and value["helper"]["executable"] not in proof["files"]:
            raise NativeError("Desktop wake verification omits its helper executable")
        for name, signature in proof["files"].items():
            if _signature(Path(name)) != signature:
                raise NativeError("Desktop wake verified application files changed")
        _recheck(value, until, peer=True)
        result = subprocess.run(["/usr/bin/open", "-g", "-a", value["app_bundle"],
                                 "codex://threads/wake/" + session_id],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=files.remaining(until), check=False)
        if result.returncode != 0:
            raise NativeError("Desktop background wake URL scheduling failed")
        return DeliveryResult("accepted", "Desktop wake URL scheduled; model admission is unconfirmed", value)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError) as error:
        return DeliveryResult("uncertain", "Desktop queued input remains unconfirmed: " + str(error))
