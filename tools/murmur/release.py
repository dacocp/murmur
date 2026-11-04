#!/usr/bin/env python3
"""Create and verify one content-addressed Murmur runtime/skill/plugin release."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ops_files import private_json, read_private_json

COMPATIBILITY = {"attention_protocol": 3, "metadata_schema": 1,
                 "metadata_backend": "postgresql-entities-v1", "mattermost": "12.0.1",
                 "postgres_major": 16, "ledger_schema": 4}
MANIFEST = "release-manifest.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def product_files(source):
    """Paths are portable across checkouts; ignored state/build files never enter."""
    source = Path(source).resolve()
    root = source.parents[1]
    result = {}
    root_files = {"install.py", "release.py", "ops_files.py", "README.md", "CONTRACT.md", "OPERATIONS.md", "native-extractions.md"}
    server_files = {"go.mod", "go.sum", "plugin.json", "compose.yaml", "bootstrap.py", "build.sh", "fixture.py", "README.md"}
    web_files = {"build.py", "index.js", "model.js", "console.css", "model.test.js", "console.test.js"}
    for base in (source, root / "skills/murmur"):
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(base)
            parts = relative.parts
            if any(part.startswith(".") or part in {"__pycache__", "dist", "node_modules"} for part in parts):
                continue
            if base == source:
                allowed = (len(parts) == 1 and path.name in root_files
                           or parts[0] == "murmur" and path.suffix == ".py"
                           or parts[0] == "tests" and (path.name.startswith("test_") and path.suffix == ".py"
                               or path.name == "__init__.py" or parts[1:3] == ("native", "fixtures") and path.suffix == ".json")
                           or parts[0] == "server" and len(parts) == 2 and (path.name in server_files
                               or path.suffix == ".go" or path.name.endswith("integration_test.py"))
                           or parts[0] == "webapp" and len(parts) == 2 and path.name in web_files)
            else:
                allowed = str(relative) in {"SKILL.md", "agents/openai.yaml"}
            if not allowed:
                continue
            if path.is_symlink():
                raise ValueError(f"Release source must not contain symlinks: {path}")
            if path.is_file():
                result[str(path.relative_to(root))] = path
    if "tools/murmur/murmur/__init__.py" not in result or "skills/murmur/SKILL.md" not in result:
        raise ValueError("Source must contain the Murmur package and skill")
    return result


def source_manifest(source):
    files = {name: digest(path.read_bytes()) for name, path in product_files(source).items()}
    try:
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                           stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        revision = "unversioned"
    return {"format": 1, "revision": revision,
            "source_sha256": digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()),
            "compatibility": dict(COMPATIBILITY), "files": files}


def build_manifest(source, output):
    value = source_manifest(source)
    # Build metadata excludes itself and all dist output from source identity.
    private_json(output, value)
    return value


def check_build(source, archive, manifest=None):
    archive = Path(archive)
    path = Path(manifest) if manifest else archive.parent / "build-manifest.json"
    value = read_private_json(path)
    current = source_manifest(source)
    if value.get("source_sha256") != current["source_sha256"] or value.get("compatibility") != COMPATIBILITY:
        raise ValueError("Plugin build does not match this product source/compatibility; rebuild it")
    if value.get("archive_sha256") != digest(archive.read_bytes()):
        raise ValueError("Plugin archive does not match its build manifest")
    return value


def finish_build(source, archive):
    path = Path(archive).parent / "build-manifest.json"
    value = read_private_json(path)
    if value["source_sha256"] != source_manifest(source)["source_sha256"]:
        raise ValueError("Product source changed during plugin build; rebuild it")
    value["archive_sha256"] = digest(Path(archive).read_bytes())
    private_json(path, value)
    return value


def snapshot(source, releases, *, archive=None):
    source = Path(source).resolve()
    # A packaged release can be installed without its Git checkout.
    packaged = source.parents[1] / MANIFEST
    packaged_plugin = None
    if packaged.exists():
        value = read_private_json(packaged)
        current = source_manifest(source)
        if (current["files"] != value.get("files") or current["source_sha256"] != value.get("source_sha256")
                or value.get("compatibility") != COMPATIBILITY):
            raise ValueError("Packaged release files have changed")
        expected = {key: value[key] for key in ("format", "revision", "source_sha256", "compatibility", "files")}
        packaged_plugin = value.get("plugin_bundle")
        if archive is None and packaged_plugin:
            archive = packaged.parent / "murmur-plugin.tar.gz"
            if digest(archive.read_bytes()) != packaged_plugin["sha256"]:
                raise ValueError("Packaged plugin archive was modified")
    else:
        expected = source_manifest(source)
    plugin = None if packaged_plugin or not archive else check_build(source, archive)
    value = {**expected, "plugin_bundle": packaged_plugin if packaged_plugin else None if plugin is None else {
        "sha256": plugin["archive_sha256"], "build": plugin}}
    identity = digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    destination = Path(releases) / identity
    if destination.exists():
        if read_private_json(destination / MANIFEST) != value:
            raise ValueError("Existing release directory has different metadata")
        for name, sha in expected["files"].items():
            if digest((destination / name).read_bytes()) != sha:
                raise ValueError("Installed release was modified; preserve it for inspection")
        if value["plugin_bundle"] and digest((destination / "murmur-plugin.tar.gz").read_bytes()) != value["plugin_bundle"]["sha256"]:
            raise ValueError("Installed plugin archive was modified; preserve it for inspection")
        return destination, value
    Path(releases).mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".release-", dir=releases))
    try:
        for name, path in product_files(source).items():
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        if source_manifest(source)["files"] != expected["files"]:
            raise ValueError("Product source changed while preparing its release")
        for name, sha in expected["files"].items():
            if digest((temporary / name).read_bytes()) != sha:
                raise ValueError("Product source changed while copying its release")
        if archive:
            shutil.copyfile(archive, temporary / "murmur-plugin.tar.gz")
            if digest((temporary / "murmur-plugin.tar.gz").read_bytes()) != value["plugin_bundle"]["sha256"]:
                raise ValueError("Plugin archive changed while copying its release")
        private_json(temporary / MANIFEST, value)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plugin-bundle", type=Path)
    parser.add_argument("--build-manifest", type=Path)
    parser.add_argument("--finish-build", type=Path)
    args = parser.parse_args()
    if args.build_manifest:
        result = build_manifest(args.source, args.build_manifest)
    elif args.finish_build:
        result = finish_build(args.source, args.finish_build)
    elif args.output:
        path, result = snapshot(args.source, args.output, archive=args.plugin_bundle)
        result = {"release": str(path), **result}
    else:
        result = source_manifest(args.source)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
