#!/usr/bin/env python3
"""Install this checkout's explicit-only Murmur integration, without networking.

Use --prefix for an isolated fixture home. Installation leaves the service inactive
unless --activate is supplied. Native hook trust stays unchanged; user-wide Claude
peer acceptance changes only with --claude-accept-peer-messages.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time

import release as releases

LABEL = "local.murmur.mattermost"
MARKER = "murmur-mattermost-owned-v1"
EVENTS = {
    "codex": ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd"),
    "claude": ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "StopFailure", "SessionEnd"),
}


class InstallError(RuntimeError):
    pass


_UNSET = object()


def _revision(path):
    if path.is_symlink() or path.exists() and not path.is_file():
        raise InstallError(f"Settings changed type; retry after review: {path}")
    return None if not path.exists() else (path.read_bytes(), path.stat().st_mode & 0o777)


def _unchanged(path, expected):
    if _revision(path) != expected:
        raise InstallError(f"Settings changed during installation; newer settings preserved. Retry: {path}")


def _atomic(path, content, mode=0o600, *, expected=_UNSET):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".murmur-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if expected is not _UNSET:
            _unchanged(path, expected)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _installation_lock(prefix):
    # All homes selecting the same native prefix share this lock. A persistent
    # temp inode avoids unlink/recreate races and contains no private state.
    identity = hashlib.sha256(str(prefix).encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f"murmur-install-{os.getuid()}-{identity}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise InstallError("Installer lock has a different owner")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError("Another Murmur installer is using this native home; retry when it finishes") from error
        yield
    finally:
        os.close(fd)


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _load(path):
    if path.is_symlink():
        raise InstallError(f"Refusing to replace symlink: {path}")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise InstallError(f"Expected a bounded JSON settings file: {path}")
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise InstallError(f"Invalid JSON in {path}") from error
    if not isinstance(value, dict):
        raise InstallError(f"Expected a JSON object in {path}")
    return value


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _owned_file(path, previous):
    if path.is_symlink() or path.exists() and not path.is_file():
        return False
    return (not path.exists() or previous is not None
            and _digest(path.read_bytes()) in previous.get("sha256s", [previous.get("sha256")]))


def _file_record(path, content, previous):
    digest = _digest(content)
    hashes = set((previous or {}).get("sha256s", []))
    if previous and previous.get("sha256"):
        hashes.add(previous["sha256"])
    return {"path": str(path), "sha256": digest, "sha256s": sorted(hashes | {digest})}


def _hooks(document, commands, additions=None):
    """Plan owned changes without moving foreign positional trust bindings."""
    document = json.loads(json.dumps(document or {}))
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("Settings hooks must be an object")
    additions = additions or {}
    replaced = set()
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            raise InstallError(f"Hook {event} must contain matcher groups")
        original_count = len(groups)
        removable = set()
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise InstallError(f"Hook {event} has an invalid matcher group")
            handlers = group["hooks"]
            owned = [
                isinstance(handler, dict) and handler.get("type") == "command"
                and handler.get("command") in commands for handler in handlers]
            if not any(owned):
                continue
            if event in additions:
                group["hooks"] = [dict(additions[event]) if ours else handler
                                  for handler, ours in zip(handlers, owned)]
                replaced.add(event)
                continue
            last_foreign = max((index for index, ours in enumerate(owned) if not ours), default=-1)
            blocked = next((index for index, ours in enumerate(owned)
                            if ours and index < last_foreign), None)
            if blocked is not None:
                raise InstallError(f"Cannot remove Murmur {event} group {group_index}, handler {blocked}: "
                                   "a later foreign handler must keep its trust/enable position")
            group["hooks"] = [handler for handler, ours in zip(handlers, owned) if not ours]
            if not group["hooks"] and set(group) == {"hooks"}:
                removable.add(group_index)
        # Empty groups preserve later group indexes. Only an owned empty suffix
        # can disappear without changing any foreign handler's state key.
        while groups and len(groups) - 1 in removable:
            groups.pop()
        if not groups and original_count:
            del hooks[event]
    for event, handler in additions.items():
        if event not in replaced:
            hooks.setdefault(event, []).append({"hooks": [dict(handler)]})
    return document


def _paths(prefix, state):
    explicit_prefix = prefix is not None
    prefix = Path(prefix or Path.home()).expanduser().resolve()
    fixture = explicit_prefix and prefix != Path.home().resolve()
    state = Path(state or (None if fixture else os.environ.get("MURMUR_HOME"))
                 or prefix / ".local/state/murmur-mattermost").expanduser().resolve()
    return prefix, state


def _platform(value=None):
    value = value or sys.platform
    if value not in {"darwin", "linux"}:
        raise InstallError("Murmur installation supports macOS and Linux only")
    return value


def _manifest(state, prefix, platform=None):
    value = _load(state / "installation.json")
    if value is not None and (value.get("version") != 1
                              or value.get("prefix") != str(prefix)
                              or value.get("state") != str(state)
                              or value.get("platform", "darwin") != _platform(platform)):
        raise InstallError("Installation manifest does not match this prefix and state")
    if value and value.get("bootstrap_inflight") and not value.get("job_owned"):
        raise InstallError("Service activation outcome is uncertain; inspect the service before retry or removal")
    return value


def _launchctl(arguments, *, timeout=10):
    return subprocess.run(["/bin/launchctl", *arguments], check=True, capture_output=True,
                          text=True, timeout=timeout)


def _mac_status(runner):
    try:
        result = runner(["print", f"gui/{os.getuid()}/{LABEL}"])
    except subprocess.CalledProcessError as error:
        if _absent(error):
            return None
        raise InstallError("Cannot establish launchd service ownership: " + _command_error(error)) from error
    except subprocess.TimeoutExpired as error:
        raise InstallError("Cannot establish launchd service ownership: probe timed out") from error
    output = getattr(result, "stdout", None)
    if not isinstance(output, str):
        raise InstallError("Cannot establish launchd service ownership: missing print output")
    values = {}
    arguments, in_arguments = [], False
    for raw in output.splitlines():
        line = raw.strip()
        if line == "arguments = {":
            in_arguments = True
        elif in_arguments and line == "}":
            in_arguments = False
        elif in_arguments:
            arguments.append(line.strip('"'))
        elif line.startswith(("path = ", "program = ")):
            key, value = line.split(" = ", 1)
            values[key] = value.strip('"')
        elif line.startswith("MURMUR_HOME => "):
            values["home"] = line.split(" => ", 1)[1].strip('"')
    if not {"path", "home"} <= values.keys() or not arguments:
        raise InstallError("Cannot establish launchd service ownership: incomplete definition")
    values["arguments"] = arguments
    return values


def _mac_preflight(runner, agent, previous):
    values = _mac_status(runner)
    if values is None:
        return
    if (not previous or not previous.get("job_owned") or not _owned_file(agent, previous.get("agent"))
            or values["path"] != str(agent)):
        raise InstallError("Foreign or unowned launchd service preserved")
    expected = plistlib.loads(agent.read_bytes())
    if (values["arguments"] != expected["ProgramArguments"]
            or values.get("program", values["arguments"][0]) != expected["ProgramArguments"][0]
            or values["home"] != expected["EnvironmentVariables"]["MURMUR_HOME"]):
        raise InstallError("Loaded launchd definition differs from the owned installation; preserved")


def _systemctl(arguments, *, timeout=10):
    return subprocess.run(["systemctl", "--user", *arguments], check=True,
                          capture_output=True, text=True, timeout=timeout)


def _unit_quote(value, *, command=False):
    # systemd unit syntax is not shell syntax. Specifiers apply to all these
    # directives; environment expansion applies to ExecStart only.
    value = str(value).replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
        "\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def _unit(launcher, state):
    return (f"# {MARKER}\n[Unit]\nDescription=Murmur coordinator\n\n[Service]\n"
            "Type=exec\nUMask=0077\n"
            f"ExecStart=/bin/sh {_unit_quote(launcher, command=True)} daemon\n"
            f"Environment={_unit_quote('MURMUR_HOME=' + str(state))}\n"
            "Restart=on-failure\nRestartSec=3\n\n[Install]\nWantedBy=default.target\n").encode()


def _linux_status(runner, unit):
    result = runner(["show", unit, "--property=LoadState,ActiveState,FragmentPath,UnitFileState,DropInPaths"])
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not {"LoadState", "ActiveState", "FragmentPath", "UnitFileState", "DropInPaths"} <= values.keys():
        raise InstallError("Cannot establish systemd service ownership")
    return values


def _linux_preflight(runner, agent, previous):
    values = _linux_status(runner, agent.name)
    if values["DropInPaths"]:
        raise InstallError("Foreign systemd drop-ins preserved")
    if values["LoadState"] == "not-found" and not values["FragmentPath"]:
        return
    inactive_owned_file = (previous and _owned_file(agent, previous.get("agent"))
                           and values["ActiveState"] == "inactive"
                           and values["UnitFileState"] == "disabled")
    if (not previous or not (previous.get("job_owned") or inactive_owned_file)
            or values["FragmentPath"] != str(agent) or values["LoadState"] != "loaded"):
        raise InstallError("Foreign or unowned systemd service preserved")


def _linux_stop(runner, agent):
    runner(["disable", "--now", agent.name])
    values = _linux_status(runner, agent.name)
    if values["ActiveState"] not in {"inactive", "failed"}:
        raise InstallError("Owned systemd service is still exiting")


def _linux_activate(runner, agent, manifest, state):
    if manifest["job_owned"]:
        _linux_stop(runner, agent)
        manifest["active"], manifest["job_owned"] = False, False
        _atomic(state / "installation.json", _json_bytes(manifest))
    runner(["daemon-reload"])
    # enable --now can partially succeed, even with a nonzero exit. Preserve
    # the uncertainty journal on every failure; never adopt an unknown job.
    manifest["bootstrap_inflight"] = True
    _atomic(state / "installation.json", _json_bytes(manifest))
    runner(["enable", "--now", agent.name])
    values = _linux_status(runner, agent.name)
    if values["FragmentPath"] != str(agent) or values["ActiveState"] != "active":
        raise InstallError("systemd did not confirm the owned coordinator is active")
    manifest["job_owned"], manifest["bootstrap_inflight"] = True, False
    try:
        _atomic(state / "installation.json", _json_bytes(manifest))
    except OSError:
        _linux_stop(runner, agent)
        manifest["job_owned"] = False
        _atomic(state / "installation.json", _json_bytes(manifest))
        raise
    manifest["active"], manifest["activation_pending"] = True, False
    _atomic(state / "installation.json", _json_bytes(manifest))


def _command_error(error):
    detail = ((error.stderr or "") + (error.stdout or "")).strip()
    return str(error) + (": " + detail[:2000] if detail else "")


def _absent(error):
    detail = ((error.stderr or "") + (error.stdout or "")).lower()
    return error.returncode in {3, 113} and any(
        phrase in detail for phrase in ("could not find service", "no such process"))


def _bootout(runner):
    try:
        runner(["bootout", f"gui/{os.getuid()}/{LABEL}"])
        return True
    except subprocess.CalledProcessError as error:
        if not _absent(error):
            raise
        return False


def _wait_absent(runner):
    target = f"gui/{os.getuid()}/{LABEL}"
    deadline = time.monotonic() + 2.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError("Owned LaunchAgent is still exiting; retry after the exact service disappears")
        try:
            runner(["print", target], timeout=remaining)
        except subprocess.CalledProcessError as error:
            if _absent(error):
                return
            raise InstallError("Cannot confirm owned LaunchAgent absence: " + _command_error(error)) from error
        except subprocess.TimeoutExpired as error:
            raise InstallError("Owned LaunchAgent absence probe timed out; no bootstrap was attempted") from error
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def _bootstrap(runner, plist, stopped_owned):
    target = f"gui/{os.getuid()}/{LABEL}"
    for attempt in range(3):
        try:
            return runner(["bootstrap", f"gui/{os.getuid()}", str(plist)])
        except subprocess.CalledProcessError as error:
            detail = ((error.stderr or "") + (error.stdout or "")).lower()
            if not stopped_owned or error.returncode != 5 or any(
                    phrase in detail for phrase in ("not permitted", "permission denied")):
                raise
            # A completed bootout can precede launchd releasing its label.
            # Retry only an explicitly absent target, never adopt a present job.
            try:
                runner(["print", target])
            except subprocess.CalledProcessError as probe:
                if not _absent(probe):
                    raise InstallError(_command_error(error) +
                        "; service absence is unverified: " + _command_error(probe)) from probe
            except subprocess.TimeoutExpired as probe:
                raise InstallError(_command_error(error) + "; service absence probe timed out") from probe
            else:
                raise InstallError(_command_error(error) +
                    "; service is present and bootstrap ownership is uncertain") from error
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))


def install(prefix=None, state=None, **options):
    prefix, state = _paths(prefix, state)
    with _installation_lock(prefix):
        return _install(prefix, state, **options)


def _install(prefix, state, *, activate=False, source=None, python=None,
             runner=None, platform=None, development_checkout=False, upgrade_legacy=False,
             plugin_bundle=None, claude_accept_peer_messages=False):
    platform = _platform(platform)
    runner = runner or (_systemctl if platform == "linux" else _launchctl)
    if activate and prefix != Path.home().resolve():
        raise InstallError("Fixture prefixes cannot activate a host service")
    source = Path(source or Path(__file__).resolve().parent).resolve()
    python = Path(python or sys.executable).resolve()
    skill = source.parents[1] / "skills/murmur"
    if not (source / "murmur/__init__.py").is_file() or not (skill / "SKILL.md").is_file():
        raise InstallError("Source checkout does not contain the Murmur package and skill")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InstallError("Captured Python interpreter is not executable")
    previous = _manifest(state, prefix, platform)
    original_source = source
    source_revision = releases.source_manifest(source)
    if previous and "runtime" not in previous and not (development_checkout or upgrade_legacy):
        raise InstallError("Legacy checkout installation preserved; use --upgrade-legacy for a release "
                           "or --development-checkout to retain explicit checkout mode")
    launcher = prefix / ".local/bin/murmur"
    plist = (prefix / ".config/systemd/user" / f"{LABEL}.service" if platform == "linux"
             else prefix / "Library/LaunchAgents" / f"{LABEL}.plist")
    if not _owned_file(launcher, (previous or {}).get("launcher")):
        raise InstallError(f"Foreign or modified launcher preserved: {launcher}")
    if not _owned_file(plist, (previous or {}).get("agent")):
        raise InstallError(f"Foreign or modified service preserved: {plist}")
    links = [prefix / f".{provider}/skills/murmur" for provider in EVENTS]
    for link in links:
        if link.exists() or link.is_symlink():
            old = next((item for item in (previous or {}).get("skills", []) if item["path"] == str(link)), {})
            if not link.is_symlink() or os.readlink(link) not in old.get("targets", [old.get("target")]):
                raise InstallError(f"Foreign or modified skill preserved: {link}")
    if prefix == Path.home().resolve():
        (_linux_preflight if platform == "linux" else _mac_preflight)(runner, plist, previous)
    runtime = {"mode": "development" if development_checkout else "release",
               "revision": source_revision["revision"], "source_sha256": source_revision["source_sha256"],
               "compatibility": source_revision["compatibility"], "python": str(python)}
    old_runtime = (previous or {}).get("runtime", {})
    if previous and (previous.get("active") or previous.get("job_owned")) and not activate and (
            old_runtime.get("mode") != runtime["mode"]
            or old_runtime.get("source_sha256") != runtime["source_sha256"]
            or old_runtime.get("python", str(python)) != str(python)
            or development_checkout and old_runtime.get("source") != str(source)):
        raise InstallError("Changing an active runtime requires explicit --activate; installed runtime preserved")
    if not development_checkout:
        directory, release_manifest = releases.snapshot(source, prefix / ".local/share/murmur/releases",
                                                        archive=plugin_bundle)
        source = directory / "tools/murmur"
        skill = directory / "skills/murmur"
        runtime.update(source=str(source), manifest=str(directory / releases.MANIFEST),
                       revision=release_manifest["revision"], source_sha256=release_manifest["source_sha256"],
                       compatibility=release_manifest["compatibility"],
                       plugin_bundle=release_manifest.get("plugin_bundle"))
    else:
        runtime["source"] = str(source)
    bootstrap = ("import runpy,sys;sys.path.insert(0," + repr(str(source))
                 + ");runpy.run_module('murmur',run_name='__main__')")
    launcher_bytes = (f"#!/bin/sh\n# {MARKER}\n"
                      + 'if [ -z "${MURMUR_HOME:-}" ]; then\n'
                      + f"  MURMUR_HOME={shlex.quote(str(state))}\nfi\nexport MURMUR_HOME\n"
                      + "exec " + shlex.join([str(python), "-c", bootstrap]) + ' "$@"\n').encode()
    active = (previous or {}).get("active", False)
    agent = {"Label": LABEL, "ProgramArguments": [str(launcher), "daemon"],
             "EnvironmentVariables": {"MURMUR_HOME": str(state)},
             "WorkingDirectory": str(source), "RunAtLoad": True,
             "KeepAlive": {"SuccessfulExit": False}, "Disabled": not (active or activate),
             "StandardOutPath": str(state / "logs/coordinator.stdout.log"),
             "StandardErrorPath": str(state / "logs/coordinator.stderr.log")}
    plist_bytes = _unit(launcher, state) if platform == "linux" else plistlib.dumps(agent)
    settings = []
    for provider in EVENTS:
        path = prefix / (".codex/hooks.json" if provider == "codex" else ".claude/settings.json")
        revision = _revision(path)
        document = _load(path)
        _unchanged(path, revision)
        old = next((item for item in (previous or {}).get("settings", [])
                    if item["path"] == str(path)), {})
        additions = {}
        for event in EVENTS[provider]:
            if provider == "claude" and event == "PostToolUse" and platform != "linux":
                continue  # The PostToolUse route is qualified only for Linux CLI.
            command = shlex.join(["/usr/bin/env", "MURMUR_HOME=" + str(state), str(launcher),
                                  "hook", provider, event]) + f" # {MARKER}"
            additions[event] = {"type": "command", "command": command,
                                "timeout": 3 if event in {"Interrupt", "SessionEnd"} else 8}
        commands = {handler["command"] for handler in additions.values()}
        updated = _hooks(document, set(old.get("commands", [])) | commands, additions)
        if provider == "claude" and claude_accept_peer_messages:
            # This explicit user-wide preference is preserved by uninstall.
            updated["crossSessionInbound"] = "accept"
        settings.append({"path": str(path), "existed": old.get("existed", document is not None),
                         "hooks_existed": old.get("hooks_existed", "hooks" in (document or {})),
                         "empty_events": old.get("empty_events", [event for event, groups in
                             (document or {}).get("hooks", {}).items() if groups == []]),
                         "commands": sorted(set(old.get("commands", [])) | commands),
                         "desired_commands": sorted(commands), "content": _json_bytes(updated),
                         "revision": revision,
                         "mode": path.stat().st_mode & 0o777 if path.exists() else 0o600})
    manifest = {"version": 1, "platform": platform, "prefix": str(prefix), "state": str(state), "active": active,
                "job_owned": (previous or {}).get("job_owned", active),
                "bootstrap_inflight": False,
                "activation_pending": activate or (previous or {}).get("activation_pending", False),
                "launcher": _file_record(launcher, launcher_bytes, (previous or {}).get("launcher")),
                "runtime": old_runtime if (active or (previous or {}).get("job_owned")) else runtime,
                "pending_runtime": runtime,
                "skills": [{"path": str(link), "target": str(skill), "targets": sorted({str(skill)} | {
                    target for item in (previous or {}).get("skills", []) if item["path"] == str(link)
                    for target in item.get("targets", [item["target"]])})} for link in links],
                "agent": _file_record(plist, plist_bytes, (previous or {}).get("agent")),
                "settings": [{key: item[key] for key in (
                    "path", "existed", "hooks_existed", "empty_events", "commands")}
                             for item in settings]}
    # Record ownership before mutations so partial installation can be removed.
    if releases.source_manifest(original_source)["files"] != source_revision["files"]:
        raise InstallError("Product source changed during installation; retry the reviewed source")
    for item in settings:
        _unchanged(Path(item["path"]), item["revision"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    (state / "logs").mkdir(exist_ok=True, mode=0o700)
    _atomic(state / "installation.json", _json_bytes(manifest))
    stopped_owned = False
    if activate and manifest["job_owned"]:
        # Quiesce the observed old generation before publishing any executable,
        # hook or skill. A later settings conflict leaves a recoverable inactive
        # installation, never an old daemon sharing state with new entry points.
        if platform == "linux":
            _linux_stop(runner, plist)
        else:
            stopped_owned = _bootout(runner)
            _wait_absent(runner)
        manifest["active"], manifest["job_owned"] = False, False
        _atomic(state / "installation.json", _json_bytes(manifest))
    _atomic(launcher, launcher_bytes, 0o755)
    for item in settings:
        _atomic(Path(item["path"]), item["content"], item["mode"], expected=item["revision"])
    for link in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink() or os.readlink(link) != str(skill):
            with tempfile.TemporaryDirectory(prefix=".murmur-link-", dir=link.parent) as directory:
                temporary = Path(directory) / "skill"
                temporary.symlink_to(skill)
                os.replace(temporary, link)
    _atomic(plist, plist_bytes)
    for item in (manifest["launcher"], manifest["agent"]):
        item["sha256s"] = [item["sha256"]]
    for item, desired in zip(manifest["settings"], settings):
        item["commands"] = desired["desired_commands"]
    for item in manifest["skills"]:
        item["targets"] = [item["target"]]
    manifest["runtime"] = runtime
    manifest.pop("pending_runtime", None)
    _atomic(state / "installation.json", _json_bytes(manifest))
    if activate and platform == "linux":
        _linux_activate(runner, plist, manifest, state)
    elif activate:
        target = f"gui/{os.getuid()}/{LABEL}"
        runner(["enable", target])
        manifest["bootstrap_inflight"] = True
        _atomic(state / "installation.json", _json_bytes(manifest))
        try:
            _bootstrap(runner, plist, stopped_owned)
        except subprocess.CalledProcessError:
            if _mac_status(runner) is not None:
                raise InstallError("Bootstrap failed with a present service; activation outcome is uncertain")
            manifest["bootstrap_inflight"] = False
            _atomic(state / "installation.json", _json_bytes(manifest))
            raise
        manifest["job_owned"], manifest["bootstrap_inflight"] = True, False
        try:
            _atomic(state / "installation.json", _json_bytes(manifest))
        except OSError:
            # This invocation has direct successful-bootstrap evidence. If the
            # ownership journal cannot record it, stop that job before failing.
            _bootout(runner)
            manifest["job_owned"] = False
            _atomic(state / "installation.json", _json_bytes(manifest))
            raise
        runner(["kickstart", target])
        manifest["active"], manifest["activation_pending"] = True, False
        _atomic(state / "installation.json", _json_bytes(manifest))
    return {"installed": True, "active": manifest["active"], "state": str(state),
            "launcher": str(launcher), "runtime": runtime,
            "codex_review": "Review the new definitions in Codex /hooks."}


def remove(prefix=None, state=None, *, runner=None, platform=None):
    prefix, state = _paths(prefix, state)
    with _installation_lock(prefix):
        return _remove(prefix, state, runner=runner, platform=platform)


def _remove(prefix, state, *, runner=None, platform=None):
    platform = _platform(platform)
    runner = runner or (_systemctl if platform == "linux" else _launchctl)
    manifest = _manifest(state, prefix, platform)
    if manifest is None:
        return {"removed": False, "preserved": []}
    agent = Path(manifest["agent"]["path"])
    if prefix == Path.home().resolve():
        (_linux_preflight if platform == "linux" else _mac_preflight)(runner, agent, manifest)
    updates = []
    for item in manifest["settings"]:
        path = Path(item["path"])
        revision = _revision(path)
        document = _load(path)
        _unchanged(path, revision)
        if document is None:
            continue
        updated = _hooks(document, set(item["commands"]))
        for event in item.get("empty_events", []):
            updated["hooks"].setdefault(event, [])
        if not item.get("hooks_existed", True) and not updated["hooks"]:
            del updated["hooks"]
        updates.append((path, updated, item["existed"], revision))
    if manifest.get("job_owned", manifest.get("active", False)):
        if prefix != Path.home().resolve() or not _owned_file(agent, manifest["agent"]):
            raise InstallError("Active service changed; review it before removal")
        if platform == "linux":
            _linux_stop(runner, agent)
        else:
            _bootout(runner)
        manifest["active"], manifest["activation_pending"], manifest["job_owned"] = False, False, False
        _atomic(state / "installation.json", _json_bytes(manifest))
    preserved = []
    for path, value, existed, revision in updates:
        if not existed and value in ({}, {"hooks": {}}):
            _unchanged(path, revision)
            path.unlink()
        else:
            _atomic(path, _json_bytes(value), revision[1], expected=revision)
    for item in manifest["skills"]:
        path = Path(item["path"])
        if path.is_symlink() and os.readlink(path) in item.get("targets", [item["target"]]):
            path.unlink()
        elif path.exists() or path.is_symlink():
            preserved.append(str(path))
    for item in (manifest["launcher"], manifest["agent"]):
        path = Path(item["path"])
        if path.exists() and _owned_file(path, item):
            path.unlink()
        elif path.exists() or path.is_symlink():
            preserved.append(str(path))
    if platform == "linux" and prefix == Path.home().resolve():
        runner(["daemon-reload"])
    (state / "installation.json").unlink()
    return {"removed": True, "preserved": preserved, "state_preserved": str(state)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, help="Home directory; fixture prefixes never activate host services")
    parser.add_argument("--state", type=Path, help="Fresh operational state; defaults to MURMUR_HOME")
    parser.add_argument("--activate", action="store_true", help="Start the host coordinator explicitly")
    parser.add_argument("--remove", action="store_true", help="Remove only this installation's owned integration")
    parser.add_argument("--development-checkout", action="store_true", help="Explicitly execute this mutable checkout instead of a release snapshot")
    parser.add_argument("--upgrade-legacy", action="store_true", help="Deliberately replace a journal-owned checkout installation with a release")
    parser.add_argument("--plugin-bundle", type=Path, help="Include a verified plugin bundle in the product release")
    parser.add_argument("--claude-accept-peer-messages", action="store_true",
                        help="Set user-wide Claude inbound peer acceptance (crossSessionInbound=accept), "
                             "including peers beyond Murmur; --remove leaves this preference unchanged")
    args = parser.parse_args(argv)
    if args.remove and args.activate:
        parser.error("--remove and --activate are mutually exclusive")
    if args.remove and args.claude_accept_peer_messages:
        parser.error("--remove and --claude-accept-peer-messages are mutually exclusive")
    try:
        result = remove(args.prefix, args.state) if args.remove else install(
            args.prefix, args.state, activate=args.activate, development_checkout=args.development_checkout,
            upgrade_legacy=args.upgrade_legacy, plugin_bundle=args.plugin_bundle,
            claude_accept_peer_messages=args.claude_accept_peer_messages)
    except (InstallError, ValueError, OSError, subprocess.SubprocessError) as error:
        detail = _command_error(error) if isinstance(error, subprocess.CalledProcessError) else str(error)
        parser.exit(1, f"Murmur installation failed: {detail}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
