"""Local verification helpers with one explicit inspection deadline."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import stat
import time

from .types import NativeError


def deadline(timeout):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise NativeError("Native inspection timeout must be finite and positive")
    return time.monotonic() + timeout


def remaining(until):
    value = until - time.monotonic()
    if value <= 0:
        raise NativeError("Native inspection exceeded its deadline")
    return value


def executable_sha256(path, until):
    """Hash a canonical regular executable, stopping between bounded reads.

    Known hashes qualify bytes; neither a version string nor an inode alone
    qualifies a build. Bracket the read against replacement or in-place change.
    """
    remaining(until)
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022:
            raise NativeError("Native executable is not a safe regular file")
        digest = hashlib.sha256()
        while True:
            remaining(until)
            block = source.read(256 * 1024)
            remaining(until)
            if not block:
                break
            digest.update(block)
        after, latest = os.fstat(source.fileno()), path.lstat()
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                  item.st_mtime_ns, item.st_ctime_ns)
        if (signature(before) != signature(after) or signature(before) != signature(latest)
                or not stat.S_ISREG(latest.st_mode) or latest.st_mode & 0o022):
            raise NativeError("Native executable changed during inspection")
    remaining(until)
    return digest.hexdigest()
