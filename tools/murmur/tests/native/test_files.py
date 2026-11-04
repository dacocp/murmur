"""Executable bytes remain qualified while local verification stays bounded."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmur.native import files
from murmur.native.types import NativeError


class VerificationFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "executable"
        self.path.write_bytes(b"qualified fixture bytes")
        self.path.chmod(0o700)

    def test_hashes_the_exact_safe_regular_file_without_writing(self):
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        digest = files.executable_sha256(self.path, files.deadline(.5))
        self.assertEqual(digest, hashlib.sha256(before[0]).hexdigest())
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))

    def test_deadline_after_a_read_prevents_build_certification(self):
        with patch.object(files.time, "monotonic", side_effect=[0, 0, 1]):
            with self.assertRaisesRegex(NativeError, "deadline"):
                files.executable_sha256(self.path, .5)

    def test_other_writable_files_and_symlinks_are_not_executable_proof(self):
        self.path.chmod(0o722)
        with self.assertRaisesRegex(NativeError, "safe regular"):
            files.executable_sha256(self.path, files.deadline(.5))
        self.path.chmod(0o700)
        alias = self.path.with_name("alias")
        alias.symlink_to(self.path)
        with self.assertRaises(OSError):
            files.executable_sha256(alias, files.deadline(.5))

    def test_timeout_validation_rejects_nonfinite_boolean_and_nonpositive_values(self):
        for timeout in (True, 0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(NativeError):
                files.deadline(timeout)
