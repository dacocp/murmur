"""Linux ownership and recovery fixtures never operate a host service manager."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("linux_installer", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class LinuxInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.prefix = Path(self.temporary.name).resolve() / "home '$() % quoted"
        self.state = self.prefix / "state"
        self.source = self.prefix / "source/tools/murmur"
        (self.source / "murmur").mkdir(parents=True)
        (self.source / "murmur/__init__.py").touch()
        skill = self.source.parents[1] / "skills/murmur"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("fixture")
        self.agent = self.prefix / ".config/systemd/user/local.murmur.mattermost.service"
        self.calls = []
        self.loaded = False
        self.active = False
        self.enabled = False
        self.dropins = ""

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, stdout=(
                "LoadState=" + ("loaded" if self.loaded else "not-found") + "\n"
                "ActiveState=" + ("active" if self.active else "inactive") + "\n"
                "FragmentPath=" + (str(self.agent) if self.loaded else "") + "\n"
                "UnitFileState=" + ("enabled" if self.enabled else "disabled") + "\n"
                "DropInPaths=" + self.dropins + "\n"))
        if args[0] == "daemon-reload":
            self.loaded = self.agent.exists()
        if args[0] == "enable":
            self.enabled = self.active = True
        if args[0] == "disable":
            self.enabled = self.active = False
        return subprocess.CompletedProcess(args, 0, stdout="")

    def install(self, **kwargs):
        return installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                 runner=self.runner, platform="linux", **kwargs)

    def remove(self):
        return installer.remove(self.prefix, self.state, runner=self.runner, platform="linux")

    def manifest(self):
        return json.loads((self.state / "installation.json").read_text())

    def test_inactive_fixture_escapes_unit_syntax_and_never_calls_systemctl(self):
        self.install()
        self.assertEqual(self.calls, [])
        value = self.agent.read_text()
        self.assertIn("$$() %% quoted", value)
        self.assertIn("UMask=0077", value)
        self.assertFalse((self.prefix / "Library").exists())
        hooks = json.loads((self.prefix / ".claude/settings.json").read_text())["hooks"]
        self.assertIn("PostToolUse", hooks)
        self.assertEqual(self.manifest()["platform"], "linux")
        self.assertTrue(self.remove()["removed"])
        self.assertEqual(self.calls, [])

    def test_fixture_activation_rejected_before_writes(self):
        with self.assertRaisesRegex(installer.InstallError, "Fixture prefixes"):
            self.install(activate=True)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls, [])

    def test_host_lifecycle_reloads_owned_service_and_preserves_settings(self):
        foreign = self.prefix / ".claude/settings.json"
        foreign.parent.mkdir()
        foreign.write_text('{"model":"preserved"}')
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            self.assertTrue(self.active)
            self.assertTrue(self.manifest()["job_owned"])
            self.calls.clear()
            self.install(activate=True)
            self.assertEqual([a[0] for a in self.calls],
                             ["show", "disable", "show", "daemon-reload", "enable", "show"])
            self.assertTrue(self.remove()["removed"])
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)
        self.assertFalse(self.loaded)
        self.assertEqual(json.loads(foreign.read_text()), {"model": "preserved"})

    def test_foreign_loaded_service_and_dropins_hold_before_mutation(self):
        self.loaded = self.active = True
        with patch.object(installer.Path, "home", return_value=self.prefix):
            with self.assertRaisesRegex(installer.InstallError, "unowned"):
                self.install(activate=True)
        self.assertFalse(self.state.exists())
        self.loaded = self.active = False
        self.install()
        self.loaded = True
        self.dropins = "/foreign/override.conf"
        before = (self.state / "installation.json").read_bytes()
        with patch.object(installer.Path, "home", return_value=self.prefix):
            with self.assertRaisesRegex(installer.InstallError, "drop-ins"):
                self.install(activate=True)
        self.assertEqual((self.state / "installation.json").read_bytes(), before)

    def test_inactive_owned_definition_can_be_activated(self):
        self.install()
        self.loaded = True
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
        self.assertTrue(self.active)

    def test_partial_enable_failure_holds_retry_and_removal(self):
        normal = self.runner

        def fail(args, **kwargs):
            value = normal(args, **kwargs)
            if args[0] == "enable":
                raise subprocess.CalledProcessError(1, args, stderr="partial enable")
            return value

        with patch.object(installer.Path, "home", return_value=self.prefix):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.prefix, self.state, source=self.source, development_checkout=True, activate=True,
                                  runner=fail, platform="linux")
            self.assertTrue(self.manifest()["bootstrap_inflight"])
            self.calls.clear()
            for operation in (lambda: self.install(activate=True), self.remove):
                with self.assertRaisesRegex(installer.InstallError, "outcome is uncertain"):
                    operation()
            self.assertEqual(self.calls, [])

    def test_manual_start_or_enable_after_inactive_install_holds_every_mutation(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install()
            self.loaded = True
            before = {str(p): p.read_bytes() for p in self.prefix.rglob("*")
                      if p.is_file() and not p.is_symlink()}
            for active, enabled in ((True, False), (False, True)):
                self.active, self.enabled = active, enabled
                for operation in (lambda: self.install(), lambda: self.install(activate=True), self.remove):
                    self.calls.clear()
                    with self.subTest(active=active, enabled=enabled), self.assertRaisesRegex(
                            installer.InstallError, "unowned"):
                        operation()
                    self.assertEqual([a[0] for a in self.calls], ["show"])
                    self.assertEqual({str(p): p.read_bytes() for p in self.prefix.rglob("*")
                                      if p.is_file() and not p.is_symlink()}, before)

    def test_cross_platform_manifest_cannot_be_adopted(self):
        self.install()
        with self.assertRaisesRegex(installer.InstallError, "manifest does not match"):
            installer.remove(self.prefix, self.state, platform="darwin")


if __name__ == "__main__":
    unittest.main()
