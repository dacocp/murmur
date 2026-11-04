from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from murmur.native import process as claude_process


def procargs(path: bytes, argv: tuple[bytes, ...], *, suffix=b'ENV_SENTINEL=not argv\0', padding=b'\0\0'):
    return struct.pack('@i', len(argv)) + path + b'\0' + padding + b'\0'.join(argv) + b'\0' + suffix


class FakeKernel:
    def __init__(self, payload, path, *, argmax=1048576, sysctl_error=None, path_error=None,
                 arg_size=None, path_count=None, terminate=True):
        self.payload, self.path = payload, path
        self.argmax, self.sysctl_error, self.path_error = argmax, sysctl_error, path_error
        self.arg_size, self.path_count, self.terminate = arg_size, path_count, terminate
        self.calls = []
        self.argument_buffer = None

    def sysctl(self, mib, length, buffer, size, new, new_size):
        key = tuple(mib[i] for i in range(length))
        self.calls.append(key)
        if new is not None or new_size != 0:
            raise AssertionError('inspection must be read only')
        count = ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))
        if key == (1, 8):
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))[0] = self.argmax
            count[0] = ctypes.sizeof(ctypes.c_int)
            return 0
        if key != (1, 49, 4321):
            raise AssertionError('unexpected process request')
        self.argument_buffer = buffer
        if count[0] != self.argmax + ctypes.sizeof(ctypes.c_int):
            raise AssertionError('argc needs space beyond kernel ARG_MAX')
        ctypes.memmove(buffer, self.payload, min(len(self.payload), count[0]))
        count[0] = self.arg_size if self.arg_size is not None else len(self.payload)
        if self.sysctl_error:
            ctypes.set_errno(self.sysctl_error)
            return -1
        return 0

    def pidpath(self, pid, buffer, capacity):
        self.calls.append(('pidpath', pid))
        if self.path_error:
            ctypes.set_errno(self.path_error)
            return 0
        encoded = self.path + (b'\0' if self.terminate else b'')
        ctypes.memmove(buffer, encoded, min(len(encoded), capacity))
        return self.path_count if self.path_count is not None else len(self.path)


class ClaudeProcessTests(unittest.TestCase):
    def inspect_fake(self, kernel):
        with patch.object(claude_process.sys, 'platform', 'darwin'), patch.object(
                claude_process, '_functions', return_value=(kernel.sysctl, kernel.pidpath)):
            return claude_process.inspect(4321)

    def test_actual_argc_preserves_spaces_empty_arguments_and_ignores_environment(self):
        expected = (b'claude display name', b'--resume', b'canonical-uuid', b'', b'path with spaces', b'--fork-session')
        raw = procargs(b'/opt/claude', expected, suffix=b'CLAUDE_CODE_SESSION_ID=secret\0--resume\0env-parent\0\xff')
        executable, argv = claude_process._parse_procargs(raw)
        self.assertEqual(executable, '/opt/claude')
        self.assertEqual(argv, tuple(os.fsdecode(value) for value in expected))
        self.assertFalse(any('secret' in value or 'env-parent' in value for value in argv))

    def test_empty_argv_zero_padding_ambiguity_requires_caller_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'binary'
            binary.write_text('fixture')
            path = os.fsencode(binary)
            # The kernel's argc + NUL-padded layout has no delimiter that can
            # distinguish this empty argv[0] from the preceding alignment NULs.
            kernel = FakeKernel(procargs(path, (b'',), suffix=b'ENV_SENTINEL=fixture-only\0'), path)
            executable, argv = self.inspect_fake(kernel)
            self.assertEqual(executable, str(binary.resolve()))
            self.assertEqual(argv, ('ENV_SENTINEL=fixture-only',))
            # This is deliberately unsupported launch metadata. The native
            # adapter must reject its first field before interpreting selectors.
            self.assertNotEqual(argv[0], 'claude')
            self.assertFalse(argv[0].startswith('/'))

    def test_non_utf8_arguments_are_preserved_without_lossy_replacement(self):
        _, argv = claude_process._parse_procargs(procargs(b'/opt/claude', (b'claude', b'\xffname')))
        self.assertEqual(os.fsencode(argv[1]), b'\xffname')

    def test_zero_alignment_padding_and_no_environment_suffix_are_valid(self):
        for padding in (b'', b'\0' * 100):
            with self.subTest(padding=len(padding)):
                self.assertEqual(claude_process._parse_procargs(procargs(
                    b'/opt/claude', (b'claude', b'', b'last'), padding=padding, suffix=b'')),
                    ('/opt/claude', ('claude', '', 'last')))

    def test_malformed_or_truncated_argument_data_is_rejected_without_dump(self):
        marker = b'PRIVATE_ENV_SENTINEL'
        cases = [b'abc', struct.pack('@i', 0) + marker, struct.pack('@i', -1) + marker,
                 struct.pack('@i', claude_process._MAX_ARGC + 1) + marker,
                 struct.pack('@i', 1) + b'/path-without-terminator' + marker,
                 struct.pack('@i', 1) + b'relative\0argv\0' + marker,
                 struct.pack('@i', 1) + b'/path\0\0\0',
                 struct.pack('@i', 2) + b'/path\0arg\0' + marker]
        for raw in cases:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError) as raised:
                claude_process._parse_procargs(raw)
            self.assertNotIn(marker.decode(), str(raised.exception))

    def test_buffer_string_and_path_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            claude_process._parse_procargs(b'x' * (claude_process._MAX_ARG_BYTES + 5))
        with patch.object(claude_process, '_MAX_STRING_BYTES', 5):
            with self.assertRaises(ValueError):
                claude_process._parse_procargs(procargs(b'/path', (b'arg123',)))
        with patch.object(claude_process, '_PATH_BUFFER_BYTES', 8):
            with self.assertRaises(ValueError):
                claude_process._parse_procargs(procargs(b'/long/path', (b'arg',)))

    def test_fake_kernel_crosschecks_canonical_executable_and_never_returns_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'binary'
            executable.write_bytes(b'fixture')
            link = Path(directory) / 'alias'
            link.symlink_to(executable)
            args = (b'argv-zero-is-not-the-executable', b'--resume', b'parent')
            kernel = FakeKernel(procargs(os.fsencode(link), args), os.fsencode(executable))
            result = self.inspect_fake(kernel)
            self.assertEqual(result, (str(executable.resolve()), tuple(os.fsdecode(v) for v in args)))
            self.assertEqual(kernel.calls, [(1, 8), (1, 49, 4321), ('pidpath', 4321)])
            self.assertEqual(kernel.argument_buffer.raw, b'\0' * len(kernel.argument_buffer))

    def test_executable_mismatch_and_nonfile_path_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'first', Path(directory) / 'second'
            first.write_text('one')
            second.write_text('two')
            for saved, actual in ((first, second), (first, Path(directory)), (first, Path(directory) / 'missing')):
                kernel = FakeKernel(procargs(os.fsencode(saved), (b'argv',)), os.fsencode(actual))
                with self.subTest(actual=actual.name), self.assertRaises(ValueError):
                    self.inspect_fake(kernel)

    def test_kernel_failures_preserve_errno_and_discard_partial_output(self):
        raw = procargs(b'/fake', (b'--resume', b'PRIVATE_ARG_SENTINEL'), suffix=b'PRIVATE_ENV_SENTINEL')
        for code in (errno.EPERM, errno.ESRCH, errno.ENOMEM):
            kernel = FakeKernel(raw, b'/fake', sysctl_error=code)
            with self.subTest(code=code), self.assertRaises(OSError) as raised:
                self.inspect_fake(kernel)
            self.assertEqual(raised.exception.errno, code)
            self.assertNotIn('PRIVATE', str(raised.exception))
            self.assertNotIn(('pidpath', 4321), kernel.calls)
            self.assertEqual(kernel.argument_buffer.raw, b'\0' * len(kernel.argument_buffer))
        kernel = FakeKernel(raw, b'/fake', path_error=errno.EACCES)
        with self.assertRaises(OSError) as raised:
            self.inspect_fake(kernel)
        self.assertEqual(raised.exception.errno, errno.EACCES)

    def test_invalid_argmax_and_returned_sizes_are_rejected(self):
        raw = procargs(b'/fake', (b'argv',))
        for argmax in (0, 4, -1, claude_process._MAX_ARG_BYTES + 1):
            kernel = FakeKernel(raw, b'/fake', argmax=argmax)
            with self.subTest(argmax=argmax), self.assertRaises(ValueError):
                self.inspect_fake(kernel)
            self.assertEqual(kernel.calls, [(1, 8)])
        for returned in (0, 3, 1048581):
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                self.inspect_fake(FakeKernel(raw, b'/fake', arg_size=returned))
        # Parsing only copied bytes must reject truncation even though allocation
        # starts zeroed and would otherwise supply a fabricated terminator.
        raw = procargs(b'/fake', (b'argv',), suffix=b'')
        with self.assertRaises(ValueError):
            self.inspect_fake(FakeKernel(raw, b'/fake', arg_size=len(raw) - 1))

    def test_pidpath_terminator_count_variants_and_bad_lengths(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'binary'
            binary.write_text('fixture')
            path = os.fsencode(binary)
            raw = procargs(path, (b'argv',))
            for count in (len(path), len(path) + 1):
                with self.subTest(count=count):
                    self.assertEqual(self.inspect_fake(FakeKernel(raw, path, path_count=count))[0], str(binary.resolve()))
            for options in ({'terminate': False}, {'path_count': len(path) + 2},
                            {'path_count': len(path) - 1}, {'path_count': 4097}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.inspect_fake(FakeKernel(raw, path, **options))

    def test_invalid_pid_and_other_platform_fail_before_loading_kernel_apis(self):
        with patch.object(claude_process, '_functions', side_effect=AssertionError('API loaded')):
            for pid in (None, True, '123', 0, -1, 2**31):
                with self.subTest(pid=pid), self.assertRaises(ValueError):
                    claude_process.inspect(pid)
            with patch.object(claude_process.sys, 'platform', 'win32'):
                with self.assertRaises(OSError) as raised:
                    claude_process.inspect(4321)
                self.assertEqual(raised.exception.errno, errno.ENOTSUP)


if __name__ == '__main__':
    unittest.main()
