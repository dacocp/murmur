"""Guarded package fixtures; no host native process, network or settings."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from murmur import qualified_cli as cli


class QualifiedCLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve() / "home with 'spaces $()"
        self.home.mkdir()
        self.source = self.home / "native-source"
        self.source.mkdir(mode=0o700)
        binary = self.source / "bin/fixture"
        binary.parent.mkdir(mode=0o700)
        binary.write_text(f"#!{sys.executable}\nimport json,os,sys\n"
                          "if sys.argv[1:] == ['--version']: print('codex-cli 0.164.0')\n"
                          "else: print(json.dumps({'args':sys.argv[1:], 'route':os.environ.get('MURMUR_CODEX_BINARY'),"
                          "'updater':os.environ.get('DISABLE_AUTOUPDATER'), 'mask':os.umask(0o077),"
                          "'home':os.environ.get('MURMUR_HOME')}))\n")
        binary.chmod(0o700)
        (self.source / "helper").write_text("qualified helper")
        (self.source / "helper").chmod(0o600)
        (self.source / "fixture").symlink_to("bin/fixture")
        self.profile = {"version": "0.164.0", "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                        "entrypoint": "bin/fixture", "source": "native-source", "package": True,
                        "tree_sha256": cli._digest(cli._tree(self.source))}

    def prepare(self):
        return cli.prepare(self.home, "codex", self.profile)

    def test_full_package_survives_source_removal_and_rejects_helper_changes(self):
        ready = self.prepare()
        self.assertEqual(ready["status"], "ready")
        shutil.rmtree(self.source)
        self.assertEqual(self.prepare(), ready)
        package = Path(ready["binary"]).parents[1]
        self.assertTrue((package / "fixture").is_symlink())
        (package / "helper").write_text("changed helper")
        # Updating the editable cache manifest cannot qualify changed helpers.
        manifest = package.parent / cli.MANIFEST
        value = json.loads(manifest.read_text())
        value["files"] = cli._tree(package)
        manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(cli.QualifiedCLIError, "changed"):
            self.prepare()
        self.assertEqual((package / "helper").read_text(), "changed helper")

    def test_unknown_cache_and_external_symlink_are_preserved(self):
        destination = cli.cache_path(self.home, "codex", self.profile)
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(cli.QualifiedCLIError, "manifest"):
            self.prepare()
        self.assertTrue(destination.exists())
        destination.rmdir()
        (self.source / "fixture").unlink()
        (self.source / "fixture").symlink_to(sys.executable)
        with self.assertRaisesRegex(cli.QualifiedCLIError, "escapes"):
            self.prepare()
        self.assertFalse(destination.exists())

    def test_missing_source_is_explicit_and_does_not_create_cache(self):
        shutil.rmtree(self.source)
        self.assertEqual(self.prepare(), {"status": "unavailable", "version": "0.164.0"})
        with self.assertRaisesRegex(cli.QualifiedCLIError, "unavailable"):
            cli.verify(self.home, "codex", self.profile)

    def test_concurrent_unknown_empty_cache_keeps_its_inode(self):
        destination = cli.cache_path(self.home, "codex", self.profile)
        original = cli._publish
        observed = []
        def interleave(temporary, target):
            target.mkdir()
            observed.append(target.stat().st_ino)
            return original(temporary, target)
        with patch.object(cli, "_publish", side_effect=interleave), self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(destination.stat().st_ino, observed[0])
        self.assertEqual(list(destination.iterdir()), [])

    def test_interrupted_publication_stays_unusable_and_is_preserved(self):
        destination = cli.cache_path(self.home, "codex", self.profile)
        rename = cli.os.rename
        def fail_manifest(source, target):
            if Path(source).name == cli.MANIFEST:
                raise OSError("fixture manifest publication interrupted")
            return rename(source, target)
        with patch.object(cli.os, "rename", side_effect=fail_manifest), self.assertRaises(OSError):
            self.prepare()
        self.assertTrue((destination / "package/helper").is_file())
        with self.assertRaisesRegex(cli.QualifiedCLIError, "manifest"):
            self.prepare()

    def test_bad_source_or_cache_symlink_never_publishes(self):
        (self.source / "helper").write_text("changed")
        with self.assertRaisesRegex(cli.QualifiedCLIError, "exact qualified"):
            self.prepare()
        self.assertFalse(cli.cache_path(self.home, "codex", self.profile).exists())
        native = self.home / ".local/share/murmur/native-cli"
        shutil.rmtree(native)
        native.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(cli.QualifiedCLIError, "symlink"):
            self.prepare()

    def test_guard_checks_version_and_keeps_argument_identity_and_child_environment(self):
        ready = self.prepare()
        expected_args = ["--resume", "not-a-created-session", "spaces ' $()"]
        with patch.object(cli, "profiles", return_value={"codex": self.profile}), \
                patch.dict(os.environ, {"MURMUR_CODEX_BINARY": "wrong route", "MURMUR_HOME": "ordinary home"}), \
                patch.object(cli.os, "execve") as execute, patch.object(cli.os, "umask") as mask:
            cli.launch(self.home, "codex", "darwin", expected_args)
            binary, args, env = execute.call_args.args
            self.assertEqual(str(binary), ready["binary"])
            self.assertEqual(args, [ready["binary"], *expected_args])
            self.assertEqual(env["MURMUR_CODEX_BINARY"], ready["binary"])
            self.assertEqual(env["MURMUR_HOME"], "ordinary home")
            self.assertEqual(os.environ["MURMUR_CODEX_BINARY"], "wrong route")
            mask.assert_called_once_with(0o077)
            with patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "wrong version", "")), \
                    self.assertRaisesRegex(SystemExit, "version output changed"):
                cli.launch(self.home, "codex", "darwin", [])
            self.assertEqual(execute.call_count, 1)

    def test_claude_single_file_pin_and_child_only_updater_setting(self):
        binary = self.source / "claude"
        binary.write_bytes(b"fixture Claude")
        binary.chmod(0o700)
        profile = {"version": "2.1.317", "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                   "entrypoint": "claude", "source": "native-source/claude", "package": False}
        cli.prepare(self.home, "claude", profile)
        with patch.object(cli, "profiles", return_value={"claude": profile}), \
                patch.dict(os.environ, {"DISABLE_AUTOUPDATER": "ordinary value"}), \
                patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "2.1.317 (Claude Code)", "")), \
                patch.object(cli.os, "execve") as execute, patch.object(cli.os, "umask"):
            cli.launch(self.home, "claude", "darwin", [])
            self.assertEqual(execute.call_args.args[2]["DISABLE_AUTOUPDATER"], "1")
            self.assertEqual(os.environ["DISABLE_AUTOUPDATER"], "ordinary value")


if __name__ == "__main__":
    unittest.main()
