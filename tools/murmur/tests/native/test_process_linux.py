"""Linux proc proof: fixed bounded entries, exact epochs and no environment."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from murmur.native import process
from murmur.native.types import IdentityError


def stat_row(pid=4321, parent=12, birth='987654', comm=b'claude ) (with spaces)\n'):
    fields = [b'S', str(parent).encode(), *([b'0'] * 17), birth.encode()]
    return str(pid).encode() + b' (' + comm + b') ' + b' '.join(fields) + b'\n'


class LinuxProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.proc = self.root / 'proc'
        self.directory = self.proc / '4321'
        (self.directory / 'ns').mkdir(parents=True)
        (self.proc / 'self/ns').mkdir(parents=True)
        for path in (self.directory / 'ns/pid', self.proc / 'self/ns/pid'):
            path.symlink_to('pid:[4026531836]')
        self.binary = self.root / 'claude'
        self.binary.write_bytes(b'fixture executable')
        (self.directory / 'exe').symlink_to(self.binary)
        (self.directory / 'stat').write_bytes(stat_row())
        (self.directory / 'status').write_text('Name:\tclaude\nUid:\t' + '\t'.join([str(os.getuid())] * 4) + '\n')
        (self.directory / 'cmdline').write_bytes(b'claude\0--resume\0exact-id\0\0path with spaces\0')
        self.machine = self.root / 'machine-id'
        self.machine.write_text('a' * 32 + '\n')
        for active in (patch.object(process.sys, 'platform', 'linux'),
                       patch.object(process, '_PROC_ROOT', self.proc),
                       patch.object(process, '_MACHINE_ID', self.machine)):
            active.start()
            self.addCleanup(active.stop)

    def test_birth_uses_field22_after_last_comm_parenthesis(self):
        self.assertEqual(process._parse_linux_stat(stat_row(), 4321), (12, '987654'))
        self.assertEqual(process.identity(4321), (12, os.getuid(), '987654'))
        for data in (stat_row(pid=2), b'4321 missing-comm', stat_row(birth='oops'),
                     stat_row(parent=2**31), stat_row(birth='9' * 33)):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                process._parse_linux_stat(data, 4321)

    def test_exact_executable_and_arguments_preserve_empty_strings_and_no_environment(self):
        (self.directory / 'environ').write_bytes(b'PRIVATE_SENTINEL=never inspect\0')
        original = process._linux_read
        names = []
        def read(directory, name, bound):
            names.append(name)
            return original(directory, name, bound)
        with patch.object(process, '_linux_read', side_effect=read), \
                patch.object(process, '_functions', side_effect=AssertionError('Mac API')):
            self.assertEqual(process.inspect(4321), (str(self.binary),
                             ('claude', '--resume', 'exact-id', '', 'path with spaces')))
        self.assertEqual(set(names), {'stat', 'status', 'cmdline'})
        self.assertEqual(process.pid_domain(), 'linux:' + 'a' * 32 + ':pid:[4026531836]')

    def test_argument_bounds_and_exact_empty_argv_zero(self):
        self.assertEqual(process._parse_linux_cmdline(b'\0arg\0'), ('', 'arg'))
        self.assertEqual(process._parse_linux_cmdline(b'claude\0\xffname\0'), ('claude', os.fsdecode(b'\xffname')))
        for data in (b'', b'no final nul', b'x' * (process._MAX_ARG_BYTES + 1)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                process._parse_linux_cmdline(data)
        with patch.object(process, '_MAX_ARGC', 1), self.assertRaises(ValueError):
            process._parse_linux_cmdline(b'a\0b\0')
        with patch.object(process, '_MAX_STRING_BYTES', 2), self.assertRaises(ValueError):
            process._parse_linux_cmdline(b'abc\0')

    def test_owner_namespace_and_uid_ambiguity_fail_closed(self):
        for content in ('Uid:\t1\t2\t1\t1\n', 'Uid:\t1\t1\t1\n',
                        'Uid:\tnope\tnope\tnope\tnope\n', 'Name:\tonly\n',
                        'Uid:\t4294967296\t4294967296\t4294967296\t4294967296\n'):
            (self.directory / 'status').write_text(content)
            with self.subTest(content=content), self.assertRaises((ValueError, IdentityError)):
                process.identity(4321)
        (self.directory / 'status').write_text('Uid:\t' + '\t'.join([str(os.getuid() + 1)] * 4) + '\n')
        with self.assertRaisesRegex(ValueError, 'another owner'):
            process.inspect(4321)
        (self.directory / 'status').write_text('Uid:\t' + '\t'.join([str(os.getuid())] * 4) + '\n')
        (self.directory / 'ns/pid').unlink()
        (self.directory / 'ns/pid').symlink_to('pid:[99]')
        with self.assertRaisesRegex(ValueError, 'another PID namespace'):
            process.inspect(4321)

    def test_reused_pid_and_argument_rewrite_cannot_return_an_inspection(self):
        with patch.object(process, '_linux_identity', side_effect=[
                (12, os.getuid(), '987654'), (12, os.getuid(), '987655')]):
            with self.assertRaisesRegex(ValueError, 'changed during inspection'):
                process.inspect(4321)
        original = process._linux_read
        calls = 0
        def read(directory, name, bound):
            nonlocal calls
            if name == 'cmdline':
                calls += 1
                if calls == 2:
                    return b'changed\0'
            return original(directory, name, bound)
        with patch.object(process, '_linux_read', side_effect=read), self.assertRaises(ValueError):
            process.inspect(4321)

    def test_symlinked_pid_and_proc_entries_are_rejected(self):
        (self.proc / '9999').symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            process.inspect(9999)
        (self.directory / 'cmdline').unlink()
        (self.directory / 'cmdline').symlink_to(self.directory / 'status')
        with self.assertRaises(OSError):
            process.inspect(4321)

    def test_replaced_executable_inode_is_rejected(self):
        other = self.root / 'other'
        other.write_bytes(b'different image')
        with patch.object(process, '_canonical', return_value=str(other)), \
                self.assertRaisesRegex(ValueError, 'replaced on disk'):
            process.inspect(4321)

    def test_machine_and_namespace_metadata_are_required_and_bounded(self):
        for data in (b'', b'x' * 200, b'A' * 32, b'a' * 31, b'a' * 32 + b'\nextra'):
            self.machine.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                process.pid_domain()
        self.machine.write_text('a' * 32)
        (self.proc / 'self/ns/pid').unlink()
        (self.proc / 'self/ns/pid').symlink_to('unverified namespace')
        with self.assertRaises(ValueError):
            process.pid_domain()


@unittest.skipUnless(sys.platform == 'linux', 'actual /proc proof requires Linux')
class ActualLinuxProcessTests(unittest.TestCase):
    def test_child_identity_argv_and_executable_without_environment(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'spaces here', ''],
                                 env={**os.environ, 'MURMUR_PRIVATE_SENTINEL': 'must not be inspected'})
        try:
            parent, uid, birth = process.identity(child.pid)
            self.assertEqual((parent, uid), (os.getpid(), os.getuid()))
            raw = Path(f'/proc/{child.pid}/stat').read_text()
            self.assertEqual(birth, raw[raw.rfind(')') + 2:].split()[19])
            binary, arguments = process.inspect(child.pid)
            self.assertEqual(binary, str(Path(sys.executable).resolve()))
            self.assertEqual(arguments[-2:], ('spaces here', ''))
            self.assertNotIn('must not be inspected', arguments)
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
