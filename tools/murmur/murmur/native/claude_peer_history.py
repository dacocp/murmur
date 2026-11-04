"""Ephemeral structural indexes for large Desktop histories.

An index is a parsing optimization, never evidence that old bytes stayed put.
Every published view freshly hashes its entire indexed frontier. Original
records are read lazily on that same file descriptor, and the caller must leave
the context successfully before using a result as native admission evidence.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from . import files
from .claude_context_receipts import _MAX_LINE, _records
from .types import NativeError


MAX_BYTES = 384 * 1024 * 1024
MAX_RECORDS = 2_000_000
MAX_UUIDS = 1_000_000
_BLOCK = 1024 * 1024
# Conservative retained-object charges, including dict/array spare capacity.
# Scratch covers the bounded input/JSON record and streaming buffers per lease;
# this is an allocation budget, not a claim about Python's process RSS allocator.
_ENTRY_BYTES, _ROW_BYTES, _UUID_BYTES = 4096, 64, 256
_SCRATCH_BYTES = 128 * 1024 * 1024
_PUBLICATION_GRACE, _BUILDER_STALE = 10.0, 30.0


class HistoryPending(NativeError):
    """A deadline-bounded provisional index has not been published yet."""


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _safe(value):
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
            or value.st_mode & 0o077 or value.st_size <= 0):
        raise NativeError("Native peer history ownership, mode or size is unsupported")


def _check_file(path, descriptor, before):
    after, latest = os.fstat(descriptor), path.lstat()
    _safe(after)
    _safe(latest)
    if (_signature(before) != _signature(after) or _signature(before) != _signature(latest)
            or path.resolve(strict=True) != path):
        raise NativeError("Native peer history changed during its snapshot")


def _key(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
        return parsed.bytes if str(parsed) == value else None
    except ValueError:
        return None


class _Entry:
    def __init__(self, key):
        self.key, self.lock, self.users = key, threading.Lock(), 0
        self.last_request = self.progress_at = self.retain_until = 0
        self.scratch = False
        self.cost = _ENTRY_BYTES
        self.clear()

    def clear(self):
        self.starts, self.lengths = array('Q'), array('I')
        self.main_rows, self.ordinals = array('I'), array('i')
        self.keys, self.lookup = [], {}
        self.frontier, self.digest, self.published = 0, hashlib.sha256(), False


class HistoryCache:
    """One provisional builder per generation, with a global retained budget.

    A bounded exclusive lease serializes readers/builders of one generation.
    Previously indexed rows only extend under that lease; no consumer can see
    an extension while it uses a published frontier. Index builders enter FIFO;
    published reads of other files may proceed within the shared memory budget.
    """
    def __init__(self, *, max_bytes=MAX_BYTES, max_records=MAX_RECORDS, max_uuids=MAX_UUIDS):
        if any(type(value) is not int or value <= 0 for value in (max_bytes, max_records, max_uuids)):
            raise ValueError("History resource bounds must be positive integers")
        self.max_bytes, self.max_records, self.max_uuids = max_bytes, max_records, max_uuids
        self._lock, self._entries = threading.Lock(), OrderedDict()
        self._used, self._scratch = 0, 0
        self._waiting, self._builder = OrderedDict(), None
        self._clock = time.monotonic

    @property
    def accounted_bytes(self):
        with self._lock:
            return self._used + self._scratch

    def _reserve(self, count, keep):
        # Caller holds the short registry/accounting lock, never a file read.
        now = self._clock()
        for key, item in list(self._entries.items()):
            if self._used + self._scratch + count <= self.max_bytes:
                break
            if (item is not keep and not item.users and item is not self._builder
                    and key not in self._waiting and now >= item.retain_until):
                self._drop(item)
        if self._used + self._scratch + count > self.max_bytes:
            if keep and keep.cost + count + (_SCRATCH_BYTES if keep.scratch else 0) > self.max_bytes:
                self._release_builder(keep)
                keep.retain_until = 0
                raise NativeError("Native peer history index cannot fit its memory budget")
            raise HistoryPending("Native peer history global memory budget is in use")

    def _release_builder(self, entry):
        self._waiting.pop(entry.key, None)
        if self._builder is entry:
            self._builder = None

    def _drop(self, entry):
        self._release_builder(entry)
        self._used -= entry.cost
        del self._entries[entry.key]

    def _prune(self):
        now = self._clock()
        if self._builder is not None and not self._builder.users:
            entry = self._builder
            try:
                path = Path(entry.key[1])
                facts = path.lstat()
                _safe(facts)
                if ((facts.st_dev, facts.st_ino) != entry.key[2:] or facts.st_size < entry.frontier
                        or path.resolve(strict=True) != path):
                    raise NativeError("Native peer builder generation is gone")
            except (OSError, ValueError):
                self._drop(entry)
        for entry in list(self._waiting.values()):
            last = entry.progress_at if entry is self._builder else entry.last_request
            if not entry.users and now - last >= _BUILDER_STALE:
                self._drop(entry)

    def _admit(self, entry, size):
        if not entry.published or entry.frontier != size:
            self._waiting.setdefault(entry.key, entry)
            if self._builder is None:
                self._builder = next(iter(self._waiting.values()))
            if self._builder is not entry:
                raise HistoryPending("Native peer history is waiting for fair index admission")

    def _charge(self, entry, count):
        with self._lock:
            self._reserve(count, entry)
            entry.cost += count
            self._used += count

    def _clear(self, entry):
        with self._lock:
            self._release_builder(entry)
            self._used -= entry.cost - _ENTRY_BYTES
            entry.cost = _ENTRY_BYTES
        entry.clear()
        entry.retain_until = 0

    @contextmanager
    def _lease(self, key, size, deadline):
        with self._lock:
            self._prune()
            entry = self._entries.get(key)
            if entry is None:
                self._reserve(_ENTRY_BYTES, None)
                entry = self._entries[key] = _Entry(key)
                self._used += entry.cost
                entry.progress_at = self._clock()
            entry.last_request = self._clock()
            self._admit(entry, size)
            entry.users += 1
            self._entries.move_to_end(key)
        acquired, scratch = False, False
        try:
            acquired = entry.lock.acquire(timeout=files.remaining(deadline))
            if not acquired:
                raise HistoryPending("Native peer history generation is in use")
            with self._lock:
                # A previous lease may have cleared/published this generation
                # while we waited. Recheck admission before progressing it.
                self._admit(entry, size)
                self._reserve(_SCRATCH_BYTES, entry)
                self._scratch += _SCRATCH_BYTES
                entry.scratch = True
                scratch = True
            yield entry
        finally:
            with self._lock:
                if scratch:
                    self._scratch -= _SCRATCH_BYTES
                    entry.scratch = False
                entry.users -= 1
            if acquired:
                entry.lock.release()

    def evict(self):
        """Forget only unleased indexes; attempts and native files are untouched."""
        with self._lock:
            for key, entry in list(self._entries.items()):
                if not entry.users:
                    self._drop(entry)

    def _extend(self, stream, entry, size, deadline):
        if size < entry.frontier:
            self._clear(entry)
            raise NativeError("Native peer history was truncated")
        stream.seek(entry.frontier)
        if entry.frontier < size:
            entry.published = False
        while entry.frontier < size:
            if time.monotonic() >= deadline:
                raise HistoryPending("Native peer history strict index is warming")
            line = stream.readline(min(_MAX_LINE + 1, size - entry.frontier))
            if len(line) > _MAX_LINE:
                self._clear(entry)
                raise NativeError("Native context line exceeds its bound")
            if not line.endswith(b'\n'):
                raise HistoryPending("Native peer history reached an incomplete native record")
            try:
                records, lookup = _records(line, deadline)
            except ValueError as error:
                if time.monotonic() >= deadline:
                    raise HistoryPending("Native peer history strict index is warming") from None
                self._clear(entry)
                raise NativeError(str(error)) from error
            node = records[0][1]
            key = next(iter(lookup), None)
            key = uuid.UUID(key).bytes if key is not None else None
            if key is not None and key in entry.lookup:
                self._clear(entry)
                raise NativeError("Native context UUID is duplicated")
            if len(entry.starts) >= self.max_records or key is not None and len(entry.lookup) >= self.max_uuids:
                self._clear(entry)
                raise NativeError("Native peer history structural index exceeds its resource bounds")
            self._charge(entry, _ROW_BYTES + (_UUID_BYTES if key is not None else 0))
            row = len(entry.starts)
            entry.starts.append(entry.frontier)
            entry.lengths.append(len(line))
            entry.keys.append(key)
            main = key is not None and node.get("isSidechain") is False
            entry.ordinals.append(len(entry.main_rows) if main else -1)
            if main:
                entry.main_rows.append(row)
            if key is not None:
                entry.lookup[key] = row
            entry.digest.update(line)
            entry.frontier += len(line)
            # The next JSON decode must not overlap the previous decoded tree.
            del records, lookup, node, line

    @contextmanager
    def snapshot(self, path, binding, deadline, *, prefix=None):
        """Offer a fresh view, or keep only provisional bounded warm progress.

        ``binding`` must be the caller's verified immutable recipient/profile
        tuple. ``prefix`` is the attempt's exact (offset, SHA256) seal, not a
        caller-selected tail. No view or admission evidence is offered when
        either integrity check fails. An independently valid current index may
        remain cached when an old attempt's captured-prefix seal is refused.
        """
        files.remaining(deadline)
        path = Path(path)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise NativeError("Native peer history requires a canonical absolute path")
        if (not isinstance(binding, tuple) or not binding
                or any(not isinstance(part, str) or not part for part in binding)):
            raise NativeError("Native peer history requires an immutable recipient/profile binding")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            before = os.fstat(stream.fileno())
            _safe(before)
            captured = None
            if prefix is not None:
                if (not isinstance(prefix, tuple) or len(prefix) != 2 or type(prefix[0]) is not int
                        or not 0 < prefix[0] <= before.st_size or not isinstance(prefix[1], str)
                        or len(prefix[1]) != 64 or any(c not in '0123456789abcdef' for c in prefix[1])):
                    raise NativeError("Native peer history captured prefix is invalid or truncated")
                captured = prefix[0]
            key = (binding, str(path), before.st_dev, before.st_ino)
            with self._lease(key, before.st_size, deadline) as entry:
                view = None
                try:
                    digests = None
                    frontier = entry.frontier
                    if entry.published:
                        if frontier <= before.st_size:
                            boundaries = {frontier, captured} if captured else {frontier}
                            digests = _hash(stream, before.st_size, boundaries, deadline)
                            changed = digests[frontier] != entry.digest.hexdigest()
                        else:
                            changed = True
                        if changed:
                            self._clear(entry)
                            if prefix is not None:
                                raise NativeError("Native peer history indexed prefix was overwritten or truncated")
                            with self._lock:
                                self._admit(entry, before.st_size)
                    frontier = entry.frontier
                    try:
                        try:
                            self._extend(stream, entry, before.st_size, deadline)
                        except HistoryPending:
                            raise
                        except NativeError:
                            # Provisional byte positions can become stale before
                            # publication. Fresh capture may rebuild once; an
                            # immutable attempt never reinterprets its old seal.
                            if prefix is not None or not frontier:
                                raise
                            self._clear(entry)
                            with self._lock:
                                self._admit(entry, before.st_size)
                            self._extend(stream, entry, before.st_size, deadline)
                    finally:
                        if entry.frontier > frontier:
                            with self._lock:
                                entry.progress_at = self._clock()
                    if digests is None:
                        digests = _hash(stream, before.st_size, {captured} if captured else set(), deadline)
                    if digests[before.st_size] != entry.digest.hexdigest():
                        self._clear(entry)
                        if prefix is not None:
                            raise NativeError("Native peer history indexed prefix was overwritten")
                        with self._lock:
                            self._admit(entry, before.st_size)
                        self._extend(stream, entry, before.st_size, deadline)
                        if digests[before.st_size] != entry.digest.hexdigest():
                            self._clear(entry)
                            raise NativeError("Native peer history changed while rebuilding its index")
                    _check_file(path, stream.fileno(), before)
                    files.remaining(deadline)
                    with self._lock:
                        if not entry.published:
                            entry.retain_until = self._clock() + _PUBLICATION_GRACE
                        entry.published = True
                        self._release_builder(entry)
                    if prefix is not None and digests[captured] != prefix[1]:
                        # The current index is independently valid, but this
                        # immutable attempt is not. It cannot retain the global
                        # builder or earn delivery-sequence retention grace.
                        with self._lock:
                            entry.retain_until = 0
                        raise NativeError("Native peer history captured prefix was overwritten")
                    view = HistoryView(stream.fileno(), before, entry, deadline, digests)
                    if captured is not None:
                        view.before(captured)  # Seal boundaries are complete record frontiers.
                    yield view
                    files.remaining(deadline)
                finally:
                    if view is not None:
                        view._state[0] = False
                    _check_file(path, stream.fileno(), before)

    def warm(self, path, binding, deadline):
        """Advance strict indexing within this deadline; never native admission."""
        try:
            with self.snapshot(path, binding, deadline):
                return True
        except HistoryPending:
            return False
        except NativeError:
            if time.monotonic() >= deadline:
                return False
            raise


def _hash(stream, size, boundaries, deadline):
    digest, result, position = hashlib.sha256(), {}, 0
    stream.seek(0)
    for end in sorted({size, *boundaries}):
        while position < end:
            files.remaining(deadline)
            block = stream.read(min(_BLOCK, end - position))
            if not block:
                raise NativeError("Native peer history was truncated while hashing")
            digest.update(block)
            position += len(block)
        result[end] = digest.hexdigest()
    files.remaining(deadline)
    return result


class HistoryView:
    def __init__(self, descriptor, facts, entry, deadline, digests, *, stop=None, state=None):
        self._descriptor, self.facts, self._entry, self._deadline = descriptor, facts, entry, deadline
        self._digests, self.size = digests, facts.st_size if stop is None else stop
        self._state = state if state is not None else [True]
        self._count = bisect_left(entry.starts, self.size)

    def _check(self):
        if not self._state[0]:
            raise NativeError("Native peer history view is outside its snapshot lease")
        files.remaining(self._deadline)

    @property
    def sha256(self):
        return self.digest_at(self.size)

    def digest_at(self, offset):
        self._check()
        if offset not in self._digests or offset > self.size:
            raise NativeError("Native peer history digest was not sealed in this snapshot")
        return self._digests[offset]

    def before(self, offset):
        self._check()
        if not offset or not self._boundary(offset):
            raise NativeError("Native peer history frontier is not a complete record boundary")
        return HistoryView(self._descriptor, self.facts, self._entry, self._deadline,
                           self._digests, stop=offset, state=self._state)

    def _boundary(self, offset):
        if type(offset) is not int or not 0 <= offset <= self.size:
            return False
        row = bisect_left(self._entry.starts, offset)
        return offset == self.size or row < self._count and self._entry.starts[row] == offset

    def _row(self, value):
        self._check()
        key = _key(value)
        row = self._entry.lookup.get(key) if key is not None else None
        return row if row is not None and row < self._count else None

    def position(self, value):
        row = self._row(value)
        return self._entry.starts[row] if row is not None else None

    def node(self, value, default=None):
        row = self._row(value)
        return self._read(row) if row is not None else default

    def _read(self, row):
        self._check()
        length = self._entry.lengths[row]
        data = os.pread(self._descriptor, length, self._entry.starts[row])
        if len(data) != length or not data.endswith(b'\n'):
            raise NativeError("Native peer history lazy record changed or was truncated")
        records, _ = _records(data, self._deadline)
        return records[0][1]

    def records(self, start=0, stop=None):
        self._check()
        end = self.size if stop is None else stop
        if not self._boundary(start) or not self._boundary(end) or start > end:
            raise NativeError("Native peer history record range exceeds its frontier")
        entry = self._entry
        for row in range(bisect_left(entry.starts, start), bisect_left(entry.starts, end)):
            yield entry.starts[row], self._read(row)

    def main_position(self, value):
        row = self._row(value)
        if row is None or self._entry.ordinals[row] < 0:
            return None
        return self._entry.ordinals[row]

    def last_main(self, stop=None):
        self._check()
        end = self.size if stop is None else stop
        if not self._boundary(end):
            raise NativeError("Native peer history main frontier exceeds its snapshot")
        rows, starts = self._entry.main_rows, self._entry.starts
        # main row ordinals are sorted in physical record order.
        count = bisect_left(rows, bisect_left(starts, end))
        return self._read(rows[count - 1]) if count else None

    def previous_main(self, value):
        position = self.main_position(value)
        if position is None or not position:
            return None
        return self._read(self._entry.main_rows[position - 1])

    def main_ids_before(self, value):
        position = self.main_position(value)
        if position is None:
            return
        for ordinal in range(position):
            self._check()
            row = self._entry.main_rows[ordinal]
            yield str(uuid.UUID(bytes=self._entry.keys[row]))


_CACHE = HistoryCache()
snapshot = _CACHE.snapshot
warm = _CACHE.warm
