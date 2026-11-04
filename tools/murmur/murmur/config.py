"""Fresh local operational state, separate from the retired Murmur home."""

import json
import os
from pathlib import Path
import tempfile


def home():
    return Path(os.environ.get("MURMUR_HOME", "~/.local/state/murmur-mattermost")).expanduser()


def load(root=None):
    root = Path(root) if root else home()
    path = root / "config.json"
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"Restrict {path} to its owner (chmod 600)")
    return json.loads(path.read_text())


def save(value, root=None):
    root = Path(root) if root else home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".config-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "config.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ledger_path(root=None):
    return (Path(root) if root else home()) / "operations.sqlite3"
