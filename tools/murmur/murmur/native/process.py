"""Read native process identity, executable and argv without its environment.

KERN_PROCARGS2 cannot distinguish leading empty argv[0] from path alignment
padding. The caller must validate its supported argv[0] before interpreting any
arguments, bracket inspection with PID/UID/birth checks, and verify the pinned
executable. This module neither establishes ancestry nor interprets arguments.
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import re
import struct
import sys


_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROCARGS2 = 49
_MAX_ARG_BYTES = 1024 * 1024
_MAX_ARGC = 16384
_MAX_STRING_BYTES = 256 * 1024
_PATH_BUFFER_BYTES = 4096  # PROC_PIDPATHINFO_MAXSIZE
_INT_BYTES = ctypes.sizeof(ctypes.c_int)
_PROC_ROOT = Path('/proc')
_MACHINE_ID = Path('/etc/machine-id')


def pid_domain() -> str:
    """Return the exact supported native registry PID address domain."""
    if sys.platform == 'darwin':
        return 'darwin'
    if sys.platform != 'linux':
        raise OSError(errno.ENOTSUP, 'Native process PID domain is unsupported')
    with _MACHINE_ID.open('rb') as source:
        machine = source.read(129)
    namespace = os.readlink(_PROC_ROOT / 'self/ns/pid')
    if (len(machine) > 128 or not re.fullmatch(rb'[0-9a-f]{32}\n?', machine)
            or not re.fullmatch(r'pid:\[[0-9]{1,20}\]', namespace)):
        raise ValueError('Linux machine or PID namespace identity is unavailable')
    return 'linux:' + machine.decode().strip() + ':' + namespace


def _linux_read(directory: int, name: str, bound: int) -> bytes:
    """Read one fixed proc entry through a pinned process-directory descriptor."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
    with os.fdopen(descriptor, 'rb') as source:
        data = source.read(bound + 1)
    if not data or len(data) > bound:
        raise ValueError('Linux process metadata is empty or exceeds its bound')
    return data


def _parse_linux_stat(data: bytes, expected_pid: int) -> tuple[int, str]:
    # comm may contain spaces, newlines and closing parentheses. Everything
    # after its final ')' is the kernel's fixed, whitespace-separated tail.
    end = data.rfind(b') ')
    if end < 0 or not data.startswith(str(expected_pid).encode() + b' ('):
        raise ValueError('Linux process stat identity is malformed')
    fields = data[end + 2:].split()
    if (len(fields) < 20 or len(fields[0]) != 1
            or fields[0] not in (b'R', b'S', b'D', b'Z', b'T', b't', b'X', b'x', b'K', b'W', b'P', b'I')
            or not fields[1].isdigit() or not fields[19].isdigit()
            or len(fields[1]) > 10 or len(fields[19]) > 32):
        raise ValueError('Linux process birth metadata is malformed')
    parent = int(fields[1])
    if not 0 <= parent < 2**31:
        raise ValueError('Linux process parent PID is invalid')
    return parent, fields[19].decode('ascii')


def _linux_identity(directory: int, pid: int) -> tuple[int, int, str]:
    parent, birth = _parse_linux_stat(_linux_read(directory, 'stat', 8192), pid)
    status = _linux_read(directory, 'status', 65536)
    rows = [row for row in status.splitlines() if row.startswith(b'Uid:')]
    if len(rows) != 1:
        raise ValueError('Linux process ownership metadata is unavailable')
    owners = rows[0].split()[1:]
    if (len(owners) != 4 or any(not value.isdigit() or len(value) > 10 for value in owners)
            or len(set(owners)) != 1 or int(owners[0]) >= 2**32):
        raise ValueError('Linux process ownership is ambiguous')
    if (parent, birth) != _parse_linux_stat(_linux_read(directory, 'stat', 8192), pid):
        raise ValueError('Linux process birth changed during inspection')
    return parent, int(owners[0]), birth


def _parse_linux_cmdline(data: bytes) -> tuple[str, ...]:
    if not data or len(data) > _MAX_ARG_BYTES or not data.endswith(b'\0'):
        raise ValueError('Linux process argument vector is empty or truncated')
    arguments = data[:-1].split(b'\0')
    if len(arguments) > _MAX_ARGC or any(len(arg) > _MAX_STRING_BYTES for arg in arguments):
        raise ValueError('Linux process arguments exceed their bound')
    return tuple(os.fsdecode(arg) for arg in arguments)


def _linux_inspect(pid: int) -> tuple[str, tuple[str, ...]]:
    directory = os.open(_PROC_ROOT / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = _linux_identity(directory, pid)
        if before[1] != os.getuid():
            raise ValueError('Linux process has another owner')
        if os.readlink('ns/pid', dir_fd=directory) != os.readlink(_PROC_ROOT / 'self/ns/pid'):
            raise ValueError('Linux process belongs to another PID namespace')
        saved_path = os.readlink('exe', dir_fd=directory)
        if not saved_path.startswith('/') or len(os.fsencode(saved_path)) >= _PATH_BUFFER_BYTES:
            raise ValueError('Linux process executable path is invalid')
        executable = _canonical(saved_path)
        loaded = os.stat('exe', dir_fd=directory)
        on_disk = Path(executable).stat()
        if (loaded.st_dev, loaded.st_ino) != (on_disk.st_dev, on_disk.st_ino):
            raise ValueError('Linux process executable was replaced on disk')
        data = _linux_read(directory, 'cmdline', _MAX_ARG_BYTES)
        argv = _parse_linux_cmdline(data)
        after = _linux_identity(directory, pid)
        latest = os.stat('exe', dir_fd=directory)
        if (before != after or saved_path != os.readlink('exe', dir_fd=directory)
                or (loaded.st_dev, loaded.st_ino) != (latest.st_dev, latest.st_ino)
                or data != _linux_read(directory, 'cmdline', _MAX_ARG_BYTES)):
            raise ValueError('Linux process changed during inspection')
        return executable, argv
    finally:
        os.close(directory)


def _functions():
    system = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    proc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    sysctl = system.sysctl
    sysctl.restype = ctypes.c_int
    sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    pidpath = proc.proc_pidpath
    pidpath.restype = ctypes.c_int
    pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    return sysctl, pidpath


def _os_error(operation: str) -> OSError:
    code = ctypes.get_errno() or errno.EIO
    return OSError(code, f'{operation}: {os.strerror(code)}')


def _terminated(data: memoryview, start: int, maximum: int) -> tuple[bytes, int]:
    end_bound = min(len(data), start + maximum + 1)
    for end in range(start, end_bound):
        if data[end] == 0:
            return bytes(data[start:end]), end + 1
    raise ValueError('Process metadata contains an unterminated or oversized string')


def _parse_procargs(data: bytes | memoryview) -> tuple[str, tuple[str, ...]]:
    """Parse argc, saved execpath, padding, and exactly argc strings from XNU.

    Bytes after the declared argument count are neither decoded nor inspected.
    A leading empty argv[0] is indistinguishable from alignment padding; that
    case shifts the returned fields and can include an environment string. The
    caller must validate its supported argv[0] before interpreting these fields.
    """
    view = memoryview(data).cast('B')
    if not _INT_BYTES <= len(view) <= _MAX_ARG_BYTES + _INT_BYTES:
        raise ValueError('Process argument buffer has an invalid size')
    argc = struct.unpack_from('@i', view)[0]
    if not 1 <= argc <= _MAX_ARGC:
        raise ValueError('Process argument count is invalid or exceeds its bound')
    path, position = _terminated(view, _INT_BYTES, _PATH_BUFFER_BYTES - 1)
    if not path.startswith(b'/'):
        raise ValueError('Process executable path must be absolute')
    while position < len(view) and view[position] == 0:
        position += 1
    if position == len(view):
        raise ValueError('Process argument vector is missing')
    argv = []
    for _ in range(argc):
        value, position = _terminated(view, position, _MAX_STRING_BYTES)
        argv.append(os.fsdecode(value))
    return os.fsdecode(path), tuple(argv)


def _read_procargs(pid: int, sysctl) -> tuple[str, tuple[str, ...]]:
    mib = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
    argmax = ctypes.c_int()
    size = ctypes.c_size_t(_INT_BYTES)
    ctypes.set_errno(0)
    if sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        raise _os_error('kern.argmax read failed')
    if size.value != _INT_BYTES or not _INT_BYTES < argmax.value <= _MAX_ARG_BYTES:
        raise ValueError('Kernel argument buffer bound is invalid')
    # KERN_PROCARGS2 reserves an additional native int for argc. Supplying only
    # ARG_MAX bytes can make XNU return a truncated stack suffix with success.
    capacity = argmax.value + _INT_BYTES
    buffer = ctypes.create_string_buffer(capacity)
    size = ctypes.c_size_t(capacity)
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    try:
        ctypes.set_errno(0)
        if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            raise _os_error('kern.procargs2 read failed')
        if not _INT_BYTES <= size.value <= capacity:
            raise ValueError('Kernel returned an invalid process argument buffer size')
        return _parse_procargs(memoryview(buffer).cast('B')[:size.value])
    finally:
        # Do not retain the kernel's unconsumed environment suffix in this buffer.
        ctypes.memset(buffer, 0, capacity)


def _read_pidpath(pid: int, pidpath) -> str:
    # A nonzero fill prevents an unwritten terminator from looking valid.
    buffer = ctypes.create_string_buffer(b'\xff' * _PATH_BUFFER_BYTES, _PATH_BUFFER_BYTES)
    try:
        ctypes.set_errno(0)
        count = pidpath(pid, buffer, _PATH_BUFFER_BYTES)
        if count <= 0:
            raise _os_error('proc_pidpath read failed')
        if count > _PATH_BUFFER_BYTES:
            raise ValueError('Kernel returned an invalid executable path size')
        path, end = _terminated(memoryview(buffer).cast('B')[:min(count + 1, _PATH_BUFFER_BYTES)],
                                0, _PATH_BUFFER_BYTES - 1)
        # API versions/callers may count the terminating NUL or omit it.
        if end not in {count, count + 1} or not path.startswith(b'/'):
            raise ValueError('Kernel returned an invalid executable path')
        return os.fsdecode(path)
    finally:
        ctypes.memset(buffer, 0, _PATH_BUFFER_BYTES)


def _canonical(path: str) -> str:
    try:
        result = Path(path).resolve(strict=True)
        if not result.is_file():
            raise ValueError
        return str(result)
    except (OSError, RuntimeError, ValueError):
        raise ValueError('Process executable path cannot be resolved to a file') from None


def inspect(pid: int) -> tuple[str, tuple[str, ...]]:
    """Return canonical executable and bounded argv for one supported native PID.

    The caller must validate argv[0] as documented above. OS failures preserve
    errno with fixed diagnostics. No fallback, process-list command, environment
    enumeration, or retry is used.
    """
    if type(pid) is not int or not 0 < pid < 2**31:
        raise ValueError('A positive native process PID is required')
    if sys.platform == 'linux':
        return _linux_inspect(pid)
    if sys.platform != 'darwin':
        raise OSError(errno.ENOTSUP, 'Native process argument inspection is unsupported')
    sysctl, pidpath = _functions()
    saved_path, argv = _read_procargs(pid, sysctl)
    actual_path = _read_pidpath(pid, pidpath)
    executable = _canonical(saved_path)
    if executable != _canonical(actual_path):
        raise ValueError('Process executable changed or kernel metadata disagrees')
    return executable, argv


def identity(pid: int, timeout: float = 3) -> tuple[int, int, str]:
    """Read ppid, uid and Claude's exact platform-specific process birth token."""
    import subprocess
    from .types import IdentityError
    try:
        if type(pid) is not int or not 0 < pid < 2**31:
            raise ValueError
        if sys.platform == 'linux':
            directory = os.open(_PROC_ROOT / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                return _linux_identity(directory, pid)
            finally:
                os.close(directory)
        if sys.platform != 'darwin':
            raise ValueError
        result = subprocess.run(
            ['/bin/ps', '-o', 'pid=,ppid=,uid=,lstart=', '-p', str(pid)],
            env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=timeout, check=True)
        match = re.fullmatch(r'\s*(\d+)\s+(\d+)\s+(\d+)\s+([^\n]+?)\s*', result.stdout)
        if not match or int(match[1]) != pid:
            raise ValueError
        return int(match[2]), int(match[3]), match[4]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise IdentityError('Native process identity inspection is unavailable') from exc


def invoking(expected_pid: int, expected_birth: str | None = None, timeout: float = 3) -> dict:
    """Prove a current invoking ancestor, without consulting inherited host IDs."""
    import time
    from .types import IdentityError
    pid, seen, deadline = os.getppid(), set(), time.monotonic() + timeout
    while pid > 1 and pid not in seen and len(seen) < 64:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IdentityError('Native Claude invoking process verification timed out')
        seen.add(pid)
        parent, owner, birth = identity(pid, remaining)
        if pid == expected_pid:
            if owner != os.getuid() or (expected_birth is not None and birth != expected_birth):
                raise IdentityError('Native Claude process ownership or birth disagrees with registry')
            return {'pid': pid, 'uid': owner, 'proc_start': birth}
        pid = parent
    raise IdentityError('Native Claude PID is not a verified invoking ancestor')
