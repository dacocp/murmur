"""Small private-file boundary shared by packaging and server administration."""

import json
import os
from pathlib import Path
import stat
import tempfile


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Expected an owner-only directory (chmod 700): {path}")
    return path


def regular_file(path, *, private=False):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 8 * 1024 * 1024:
        raise ValueError(f"Expected a bounded owned regular file: {path}")
    if private and info.st_mode & 0o077:
        raise ValueError(f"Expected an owner-only file (chmod 600): {path}")


def read_private_json(path):
    regular_file(path, private=True)
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_private(path, content):
    path = Path(path)
    if path.exists() or path.is_symlink():
        regular_file(path)
    fd, temporary = tempfile.mkstemp(prefix=".murmur-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_json(path, value):
    write_private(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
