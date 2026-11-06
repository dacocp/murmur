"""Locally copied, exact-build CLI packages used by the owned launchers.

Nothing is downloaded. Native caches are retained with immutable releases so
removing the integration cannot remove files from an already running CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from .native import files

MARKER = "murmur-qualified-cli-v1"
MANIFEST = "package-manifest.json"
CLAUDE_MAC_SHA256 = "09f153b46b5df9e8b3454716d682a91901de020cbca630f61dec1a015b32b5e9"


class QualifiedCLIError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def profiles(platform):
    if platform == "darwin":
        return {
            "codex": {"version": "0.164.0", "sha256":
                "d8990e3ee7acfbc26f91265b12a1a83f0fee40d0e0f8f1f40a37d4ec21f499cf",
                "tree_sha256": "9a570292fdd3bce7f8f3e1514d913b277615a660d9bb12f22f133f8a9c229bf2",
                "entrypoint": "bin/codex", "source":
                ".codex/packages/app-server-daemon/releases/0.164.0-aarch64-apple-darwin",
                "package": True},
            "claude": {"version": "2.1.317", "sha256": CLAUDE_MAC_SHA256,
                "entrypoint": "claude", "source": ".local/share/claude/versions/2.1.317",
                "package": False},
        }
    if platform == "linux":
        return {
            "codex": {"version": "0.164.0", "sha256":
                "b93f6811d3f653f0fb83be4050d857fb9abbb111dfa2ce160563327daff7f306",
                "tree_sha256": "292da629036456d6cfcd3e8fd4687ef219eb16a96c7b4cd1ab003ba82fbc3ac6",
                "entrypoint": "bin/codex", "source":
                ".codex/packages/standalone/releases/0.164.0-x86_64-unknown-linux-musl",
                "package": True},
            "claude": {"version": "2.1.311", "sha256":
                "86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada",
                "entrypoint": "claude", "source": ".local/share/claude/versions/2.1.311",
                "package": False},
        }
    raise QualifiedCLIError("Qualified native CLIs support macOS and Linux only")


def cache_path(prefix, provider, profile):
    return Path(prefix) / ".local/share/murmur/native-cli" / provider / profile["sha256"]


def _tree(root):
    """Bind every regular file and internal symlink, including native helpers."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o022:
        raise QualifiedCLIError("Native package is missing or is a symlink")
    result, size = {}, 0
    deadline = time.monotonic() + 30
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if len(result) > 4096 or len(path.relative_to(root).parts) > 32:
            raise QualifiedCLIError("Native package exceeds the supported bounds")
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(root.resolve()):
                raise QualifiedCLIError("Native package symlink escapes its package")
            result[relative] = {"symlink": target}
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
            if size > 1024 * 1024 * 1024:
                raise QualifiedCLIError("Native package exceeds the supported size")
            result[relative] = {"sha256": files.executable_sha256(path, deadline),
                                "executable": bool(info.st_mode & 0o111)}
        elif not stat.S_ISDIR(info.st_mode):
            raise QualifiedCLIError("Native package contains a special file")
        if not stat.S_ISLNK(info.st_mode) and (info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022):
            raise QualifiedCLIError("Native package has unsafe ownership or permissions")
    return result


def _expected(profile):
    if profile["package"]:
        return profile["tree_sha256"]
    return _digest({profile["entrypoint"]: {"sha256": profile["sha256"], "executable": True}})


def _publish(temporary, destination):
    # mkdir exclusively claims the address: rename(directory) would replace an
    # unrelated empty directory appearing concurrently. The manifest is the
    # atomic readiness commit and is published only after the complete payload.
    # On interruption, preserve our partial directory for review; verify refuses
    # it rather than guessing ownership or executing partially published bytes.
    destination.mkdir(mode=0o700)
    os.rename(temporary / "package", destination / "package")
    os.rename(temporary / MANIFEST, destination / MANIFEST)


def verify(prefix, provider, profile):
    destination = cache_path(prefix, provider, profile)
    if destination.resolve() != destination or not destination.is_dir():
        raise QualifiedCLIError(f"Qualified {provider} {profile['version']} is unavailable; run the Murmur installer after restoring the qualified local package")
    manifest = destination / MANIFEST
    if (manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1024 * 1024
            or manifest.stat().st_uid != os.getuid() or manifest.stat().st_mode & 0o022):
        raise QualifiedCLIError("Qualified native cache ownership manifest is missing or unsafe")
    value = json.loads(manifest.read_text())
    tree = _tree(destination / "package")
    expected = {"marker": MARKER, "provider": provider, "version": profile["version"],
                "sha256": profile["sha256"], "entrypoint": profile["entrypoint"], "files": tree}
    if value != expected or _digest(tree) != _expected(profile):
        raise QualifiedCLIError("Qualified native package changed; preserved for review")
    binary = destination / "package" / profile["entrypoint"]
    if binary.is_symlink() or not os.access(binary, os.X_OK):
        raise QualifiedCLIError("Qualified native entrypoint is not a regular executable")
    return binary


def prepare(prefix, provider, profile):
    """Publish only verified local bytes; unavailable CLIs do not block Desktop setup."""
    destination = cache_path(prefix, provider, profile)
    if destination.exists() or destination.is_symlink():
        return {"status": "ready", "binary": str(verify(prefix, provider, profile)), "version": profile["version"]}
    source = Path(prefix) / profile["source"]
    if not source.exists():
        return {"status": "unavailable", "version": profile["version"]}
    if destination.resolve() != destination:
        raise QualifiedCLIError("Native cache parent is a symlink; preserved for review")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".native-", dir=destination.parent))
    try:
        package = temporary / "package"
        if profile["package"]:
            original = _tree(source)
            if _digest(original) != _expected(profile):
                raise QualifiedCLIError("Local native package is not the exact qualified package")
            shutil.copytree(source, package, symlinks=True)
            if _tree(source) != original:
                raise QualifiedCLIError("Local native package changed while copying")
        else:
            if source.is_symlink() or files.executable_sha256(source, time.monotonic() + 15) != profile["sha256"]:
                raise QualifiedCLIError("Local native binary is not the exact qualified build")
            package.mkdir(mode=0o700)
            shutil.copyfile(source, package / profile["entrypoint"])
            (package / profile["entrypoint"]).chmod(0o700)
        tree = _tree(package)
        if _digest(tree) != _expected(profile):
            raise QualifiedCLIError("Copied native package does not match the qualified package")
        value = {"marker": MARKER, "provider": provider, "version": profile["version"],
                 "sha256": profile["sha256"], "entrypoint": profile["entrypoint"], "files": tree}
        (temporary / MANIFEST).write_text(json.dumps(value, sort_keys=True) + "\n")
        (temporary / MANIFEST).chmod(0o600)
        _publish(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "ready", "binary": str(verify(prefix, provider, profile)), "version": profile["version"]}


def launch(prefix, provider, platform, arguments):
    try:
        profile = profiles(platform)[provider]
        binary = verify(prefix, provider, profile)
        environment = dict(os.environ)
        if provider == "claude":
            environment["DISABLE_AUTOUPDATER"] = "1"
        else:
            environment["MURMUR_CODEX_BINARY"] = str(binary)
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                                timeout=5, check=True, env=environment)
        expected = f"codex-cli {profile['version']}" if provider == "codex" else f"{profile['version']} (Claude Code)"
        if result.stdout.strip() != expected:
            raise QualifiedCLIError("Qualified native version output changed")
        os.umask(0o077)
        os.execve(binary, [str(binary), *arguments], environment)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"Murmur qualified CLI unavailable: {error}")
