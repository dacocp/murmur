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
_PROFILES = ({
    "bundle_id": "com.openai.codex",
    "version": "26.1023.51421",
    "build": "22530",
    "app_sha256": "44342669fea068c3d335eff7cc40ee110b34fa4ff3570bb03cc507bd73fe980d",
    "asar_sha256": "1c8ef1bef66e5fc416ae66eff8e6529972aa3601e27a1c7b5f61d6a292b957d3",
    "owner_sha256": "d84a54b031085f01e3ad915ea7c72ebea4dcf69e90f0d1e0e4a637e8280991d2",
    "launcher_sha256": "50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8",
}, {
    "bundle_id": "com.openai.codex",
    "version": "26.1027.30824",
    "build": "22943",
    "app_sha256": "df23c13298327375ad613bb43d69dab6706591898a026e223316763b0ad7b069",
    "asar_sha256": "36bf3d2b35e95e11852074b9ca9459b89efbecd6b8311345d060cf66de8f0ba6",
    "owner_sha256": "58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea",
    "launcher_sha256": "50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8",
}, {
    "bundle_id": "com.openai.codex",
    "version": "26.1027.51404",
    "build": "23880",
    "app_sha256": "0c1cb804f8858c8ab059a26a1640b9a66c9a5071c48f39d6618e8698da370b56",
    "asar_sha256": "52e0a65f9fea51f4287bbd98c63343761f9424517bb793652bb554764bc0bcc8",
    "owner_sha256": "58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea",
    "launcher_sha256": "50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8",
}, {
    "bundle_id": "com.openai.codex",
    "version": "26.1027.61615",
    "build": "24141",
    "app_sha256": "b3783838c86c3124d4b0fd5fa5c2d2fca327b4b4f52c7c7c369df25cfa879509",
    "asar_sha256": "bb88615052255e79b5977885f9a0cc212c268589587c79d4d3b7c4b46e881a95",
    "owner_sha256": "58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea",
    "launcher_sha256": "50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8",
}, {
    "bundle_id": "com.openai.codex",
    "version": "26.1029.61003",
    "build": "24614",
    "app_sha256": "e25946854a71aa4ccc6d5dc3d28b22e65c6882bfc28fbb3f10dc809e262b865d",
    "asar_sha256": "0c951258b7ab381ec601045ffb02159e53f424548dda18e6a4dcfd126557383b",
    "owner_sha256": "508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44",
    "launcher_sha256": "50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8",
}, {
    "bundle_id": "com.openai.codex",
    "version": "26.1103.32239",
    "build": "25471",
    "app_sha256": "3f5c9ea81862517e14a3b63e5cb988e6d7b171c77acd0b472b40f2519906ffaa",
    "asar_sha256": "8e55cbc7f0b7797ca1972cbf6d8b37f26dd8b79d0613235bc7448905a3bc7a2b",
    "owner_sha256": "a636331ed87605efc4cf84028ac2884a75363f9d10ead63cffb57ea0016d486e",
    "launcher_sha256": "50ab38ba21d0d9f8346f32f41848382f15b556190f3c7a07e885a4fb73e379c8",
})
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
    for name in ("app", "owner"):
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


def _database_files(owner, domain, until):
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
        if (not descriptors or path.resolve(strict=True) != path
                or not stat.S_ISREG(facts.st_mode) or facts.st_uid != os.getuid() or facts.st_mode & 0o022):
            raise NativeError("Desktop successor does not own the safe default native databases")
        if any((int(record.get('D', '-1'), 0), int(record.get('i', '-1'), 10)) != (facts.st_dev, facts.st_ino)
               for record in descriptors):
            raise NativeError("Desktop successor native database FD differs from its pathname")
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
        expected = {"app": str(bundle / "Contents/MacOS/ChatGPT"),
                    "owner": str(bundle / _OWNER_RELATIVE_PATH)}
        candidates = {"app": [], "owner": []}
        rows = _listing(["/bin/ps", "-axo", "pid=,uid=,comm="], until)
        if len(rows) > 16_384:
            raise NativeError("Desktop successor process inventory exceeds its bound")
        for row in rows:
            parts = row.split(None, 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                raise NativeError("Desktop successor process inventory is malformed")
            if int(parts[1]) != os.getuid():
                continue
            for kind, executable in expected.items():
                if parts[2] == executable:
                    candidates[kind].append(int(parts[0]))
        if len(candidates['app']) != 1:
            raise NativeError("Desktop successor app is absent or ambiguous")
        app, app_argv = _inspect(candidates["app"][0], until)
        eligible = []
        for pid in candidates['owner']:
            owner, argv = _inspect(pid, until)
            if (owner['executable'] == expected['owner'] and owner['parent_pid'] == app['pid']
                    and _startup_supported(argv, app_argv)):
                eligible.append(owner)
        if app['executable'] != expected['app'] or len(eligible) != 1:
            raise NativeError("Desktop successor eligible backend/startup domain is absent or ambiguous")
        owner = eligible[0]
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


def _startup_supported(argv, app_argv):
    if any(arg == "--user-data-dir" or arg.startswith("--user-data-dir=") for arg in app_argv[1:]):
        return False
    args, index, server = argv[1:], 0, 0
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
        executable = Path(app["executable"])
        if (executable.name != "ChatGPT" or executable.parent.name != "MacOS"
                or executable.parent.parent.name != "Contents"
                or executable.parent.parent.parent.suffix != ".app"):
            return None
        bundle = executable.parent.parent.parent
        if Path(owner["executable"]) != bundle / _OWNER_RELATIVE_PATH:
            return None
        result = {"schema": 1, "kind": _KIND, "app_bundle": str(bundle),
                  "app": app, "owner": owner, "pipe": _socket(values[_PIPE_ENV]),
                  "domain": _domain(values), "startup_supported": _startup_supported(argv, app_argv)}
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
    _absolute(value["app_bundle"])
    for key in ("app", "owner"):
        facts = value[key]
        if (not isinstance(facts, dict) or type(facts.get("pid")) is not int or facts["pid"] <= 1
                or facts.get("uid") != os.getuid() or not isinstance(facts.get("birth"), str) or not facts["birth"]
                or not isinstance(facts.get("argv_sha256"), str) or len(facts["argv_sha256"]) != 64):
            raise NativeError("Desktop wake process capture is invalid")
        _absolute(facts["executable"])
    if value["owner"]["parent_pid"] != value["app"]["pid"]:
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
    if (app != value["app"] or owner != value["owner"] or not _startup_supported(argv, app_argv)
            or _socket(value["pipe"]["path"]) != value["pipe"]):
        raise NativeError("Desktop wake app, backend or socket generation changed")
    if peer:
        _peer_owner(value, until)


def _signature(path):
    facts = path.lstat()
    if (not stat.S_ISREG(facts.st_mode) or facts.st_uid not in {0, os.getuid()} or facts.st_mode & 0o022):
        raise NativeError("Desktop wake application file is unsafe")
    return [facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns]


def _profile(bundle, owner, producer, until):
    info_path = bundle / "Contents/Info.plist"
    before = _signature(info_path)
    with info_path.open("rb") as source:
        data = source.read(256 * 1024 + 1)
    if len(data) > 256 * 1024 or _signature(info_path) != before:
        raise NativeError("Desktop wake app metadata changed or exceeds its bound")
    info = plistlib.loads(data)
    profile = next((profile for profile in _PROFILES
                    if info.get("CFBundleIdentifier") == profile["bundle_id"]
                    and info.get("CFBundleShortVersionString") == profile["version"]
                    and info.get("CFBundleVersion") == profile["build"]
                    and info.get("CFBundleExecutable") == "ChatGPT"), None)
    if profile is None:
        raise NativeError("Desktop wake app version is not qualified")
    paths = [(bundle / "Contents/MacOS/ChatGPT", "app_sha256"),
             (bundle / "Contents/Resources/app.asar", "asar_sha256"),
             (bundle / _OWNER_RELATIVE_PATH, "owner_sha256")]
    if Path(owner["executable"]) != paths[2][0]:
        raise NativeError("Desktop wake backend is outside the captured app")
    if producer != paths[2][0]:
        paths.append((bundle / "Contents/Resources/codex-cli/bin/codex", "launcher_sha256"))
    signatures = {str(info_path): before}
    for path, key in paths:
        initial = _signature(path)
        if files.executable_sha256(path, until) != profile[key] or _signature(path) != initial:
            raise NativeError("Desktop wake application source or backend is not qualified")
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
        profile, signatures = _profile(bundle, value["owner"], producer, until)
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
        if proof.get("profile") not in _PROFILES or not proof.get("files"):
            raise NativeError("Desktop wake requires an outside-hook verified app profile")
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
