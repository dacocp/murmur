"""Observed service, settings and release boundaries; temporary homes only."""

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import release

SPEC = importlib.util.spec_from_file_location("observed_installer", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class MacManager:
    def __init__(self):
        self.definition = None
        self.path = None
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "print":
            if self.definition is None:
                raise subprocess.CalledProcessError(113, args, stderr="Could not find service")
            value = self.definition
            output = (f"path = {self.path}\nprogram = {value['ProgramArguments'][0]}\narguments = {{\n"
                      + "\n".join(value["ProgramArguments"]) + "\n}\n"
                      + "MURMUR_HOME => " + value["EnvironmentVariables"]["MURMUR_HOME"] + "\n")
            return subprocess.CompletedProcess(args, 0, stdout=output)
        if args[0] == "bootstrap":
            self.path = Path(args[2])
            self.definition = plistlib.loads(self.path.read_bytes())
        if args[0] == "bootout":
            self.definition = None
        return subprocess.CompletedProcess(args, 0, stdout="")


class MacObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prefix = self.root / "home"
        self.state = self.prefix / "state"
        self.source = self.make_source("source", "release-a")
        self.manager = MacManager()

    def make_source(self, name, text):
        source = self.root / name / "tools/murmur"
        (source / "murmur").mkdir(parents=True)
        (source / "murmur/__init__.py").write_text("")
        (source / "murmur/__main__.py").write_text(f"print({text!r})\n")
        skill = source.parents[1] / "skills/murmur"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(text)
        return source

    def install(self, **options):
        return installer.install(self.prefix, self.state, source=self.source,
                                 platform="darwin", runner=self.manager, **options)

    def snapshot(self):
        return {str(p): (os.readlink(p) if p.is_symlink() else p.read_bytes())
                for p in self.prefix.rglob("*") if p.is_symlink() or p.is_file()}

    def test_manual_load_of_inactive_install_holds_install_and_remove(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(development_checkout=True)
            self.manager.path = self.prefix / f"Library/LaunchAgents/{installer.LABEL}.plist"
            self.manager.definition = plistlib.loads(self.manager.path.read_bytes())
            before = self.snapshot()
            for action in (lambda: self.install(development_checkout=True),
                           lambda: installer.remove(self.prefix, self.state, platform="darwin", runner=self.manager)):
                self.manager.calls.clear()
                with self.assertRaisesRegex(installer.InstallError, "unowned"):
                    action()
                self.assertEqual([a[0] for a in self.manager.calls], ["print"])
                self.assertEqual(self.snapshot(), before)

    def test_owned_observed_update_and_remove_preserve_foreign_settings(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(development_checkout=True, activate=True)
            settings = self.prefix / ".claude/settings.json"
            value = json.loads(settings.read_text())
            value["model"] = "user choice"
            settings.write_text(json.dumps(value))
            self.source = self.make_source("other-checkout", "release-b")
            self.install(development_checkout=True, activate=True)
            self.assertEqual(os.readlink(self.prefix / ".claude/skills/murmur"),
                             str(self.source.parents[1] / "skills/murmur"))
            self.assertTrue(installer.remove(self.prefix, self.state, platform="darwin", runner=self.manager)["removed"])
            self.assertEqual(json.loads(settings.read_text()), {"model": "user choice"})

    def test_stale_owned_label_and_unparseable_probes_hold_mutations(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(development_checkout=True, activate=True)
            before = self.snapshot()
            self.manager.definition["ProgramArguments"] = ["/foreign/program", "daemon"]
            with self.assertRaisesRegex(installer.InstallError, "differs"):
                self.install(development_checkout=True, activate=True)
            self.assertEqual(self.snapshot(), before)
            for outcome in (None, "incomplete", subprocess.TimeoutExpired("fixture", 1),
                            subprocess.CalledProcessError(5, "fixture", stderr="Permission denied")):
                def probe(args, **kwargs):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return subprocess.CompletedProcess(args, 0, stdout=outcome)
                with self.assertRaisesRegex(installer.InstallError, "ownership"):
                    installer.remove(self.prefix, self.state, platform="darwin", runner=probe)
                self.assertEqual(self.snapshot(), before)

    def test_prefix_lock_serializes_different_state_homes(self):
        with installer._installation_lock(self.prefix):
            with self.assertRaisesRegex(installer.InstallError, "Another Murmur installer"):
                self.install(development_checkout=True)
        self.assertFalse(self.state.exists())

    def test_concurrent_settings_edits_survive_install_and_remove(self):
        settings = self.prefix / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"model":"first"}')
        atomic = installer._atomic
        def concurrent(path, content, mode=0o600, **options):
            if path == settings:
                value = json.loads(path.read_text())
                value["new_user_setting"] = "preserve"
                path.write_text(json.dumps(value))
            return atomic(path, content, mode, **options)
        with patch.object(installer, "_atomic", side_effect=concurrent):
            with self.assertRaisesRegex(installer.InstallError, "newer settings preserved"):
                self.install(development_checkout=True)
        self.assertEqual(json.loads(settings.read_text())["new_user_setting"], "preserve")
        self.install(development_checkout=True)
        def during_remove(path, content, mode=0o600, **options):
            if path == settings:
                value = json.loads(path.read_text())
                value["another_user_setting"] = "also preserve"
                path.write_text(json.dumps(value))
            return atomic(path, content, mode, **options)
        with patch.object(installer, "_atomic", side_effect=during_remove):
            with self.assertRaisesRegex(installer.InstallError, "newer settings preserved"):
                installer.remove(self.prefix, self.state, platform="darwin")
        self.assertEqual(json.loads(settings.read_text())["another_user_setting"], "also preserve")

    def test_source_changed_during_planning_is_not_installed(self):
        hooks = installer._hooks
        def change_source(*args, **kwargs):
            (self.source / "murmur/__main__.py").write_text("print('changed')\n")
            return hooks(*args, **kwargs)
        with patch.object(installer, "_hooks", side_effect=change_source):
            with self.assertRaisesRegex(installer.InstallError, "source changed"):
                self.install(development_checkout=True)
        self.assertFalse((self.prefix / ".local/bin/murmur").exists())

    def test_release_snapshot_and_deliberate_legacy_conversion(self):
        self.install(development_checkout=True)
        path = self.state / "installation.json"
        legacy = json.loads(path.read_text())
        del legacy["runtime"]
        path.write_text(json.dumps(legacy))
        before = self.snapshot()
        with self.assertRaisesRegex(installer.InstallError, "upgrade-legacy"):
            self.install()
        self.assertEqual(self.snapshot(), before)
        result = self.install(upgrade_legacy=True)
        self.assertEqual(result["runtime"]["mode"], "release")
        installed = Path(result["runtime"]["source"])
        (self.source / "murmur/__main__.py").write_text("print('changed source')\n")
        returned = subprocess.check_output([str(self.prefix / ".local/bin/murmur")], text=True)
        self.assertEqual(returned.strip(), "release-a")
        self.assertEqual((installed / "murmur/__main__.py").read_text(), "print('release-a')\n")
        self.assertTrue(Path(result["runtime"]["manifest"]).is_file())

    def test_active_release_switch_needs_explicit_activation(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            before = self.snapshot()
            (self.source / "murmur/__main__.py").write_text("print('new release')\n")
            with self.assertRaisesRegex(installer.InstallError, "explicit --activate"):
                self.install()
            self.assertEqual(self.snapshot(), before)

    def test_active_release_cannot_silently_switch_to_development(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            old = dict(self.manager.definition)
            before = self.snapshot()
            self.source = self.make_source("development-b", "new checkout")
            with self.assertRaisesRegex(installer.InstallError, "explicit --activate"):
                self.install(development_checkout=True)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.manager.definition, old)

    def test_failed_active_upgrade_quiesces_old_runtime_before_publication(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            settings = self.prefix / ".claude/settings.json"
            self.source = self.make_source("next-release", "release-b")
            atomic = installer._atomic
            def concurrent(path, content, mode=0o600, **options):
                if path == settings:
                    self.assertIsNone(self.manager.definition, "old daemon must be stopped before publishing new hooks")
                    value = json.loads(path.read_text())
                    value["new_user_setting"] = "preserve"
                    path.write_text(json.dumps(value))
                return atomic(path, content, mode, **options)
            with patch.object(installer, "_atomic", side_effect=concurrent):
                with self.assertRaisesRegex(installer.InstallError, "newer settings preserved"):
                    self.install(activate=True)
            journal = json.loads((self.state / "installation.json").read_text())
            self.assertFalse(journal["active"])
            self.assertFalse(journal["job_owned"])
            self.assertTrue(journal["activation_pending"])
            self.assertIsNone(self.manager.definition)
            self.assertEqual(json.loads(settings.read_text())["new_user_setting"], "preserve")
            self.install(activate=True)
            self.assertIsNotNone(self.manager.definition)

    def test_failed_stop_retains_old_runtime_identity_and_blocks_unactivated_switch(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            original = self.install(activate=True)["runtime"]
            launcher = self.prefix / ".local/bin/murmur"
            before = launcher.read_bytes()
            self.source = self.make_source("next-release", "release-b")
            manager = self.manager
            def denied(args, **kwargs):
                if args[0] == "bootout":
                    raise subprocess.CalledProcessError(5, args, stderr="Permission denied")
                return manager(args, **kwargs)
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.prefix, self.state, source=self.source, platform="darwin", runner=denied,
                                  activate=True)
            journal = json.loads((self.state / "installation.json").read_text())
            self.assertEqual(journal["runtime"], original)
            self.assertIn("pending_runtime", journal)
            with self.assertRaisesRegex(installer.InstallError, "explicit --activate"):
                self.install()
            self.assertEqual(launcher.read_bytes(), before)

    def test_owned_but_not_fully_activated_job_also_blocks_runtime_switch(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            path = self.state / "installation.json"
            journal = json.loads(path.read_text())
            # Successful bootstrap can own a running RunAtLoad job before a
            # later kickstart/final journal failure records full activation.
            journal["active"] = False
            path.write_text(json.dumps(journal))
            self.source = self.make_source("next-release", "release-b")
            before = self.snapshot()
            with self.assertRaisesRegex(installer.InstallError, "explicit --activate"):
                self.install()
            self.assertEqual(self.snapshot(), before)

    def test_product_release_verifies_bundled_archive_on_reuse_and_repackaging(self):
        dist = self.source / "server/dist"
        dist.mkdir(parents=True)
        archive = dist / "murmur-0.1.0.tar.gz"
        archive.write_bytes(b"archive-a")
        release.build_manifest(self.source, dist / "build-manifest.json")
        release.finish_build(self.source, archive)
        directory, manifest = release.snapshot(self.source, self.root / "releases", archive=archive)
        self.assertEqual(manifest["plugin_bundle"]["sha256"], release.digest(b"archive-a"))
        copied, other = release.snapshot(directory / "tools/murmur", self.root / "copied-releases")
        self.assertEqual(other, manifest)
        self.assertEqual((copied / "murmur-plugin.tar.gz").read_bytes(), b"archive-a")
        (directory / "murmur-plugin.tar.gz").write_bytes(b"archive-b")
        with self.assertRaisesRegex(ValueError, "archive was modified"):
            release.snapshot(self.source, self.root / "releases", archive=archive)
        with self.assertRaisesRegex(ValueError, "archive was modified"):
            release.snapshot(directory / "tools/murmur", self.root / "copied-releases")

    def test_release_allowlist_excludes_operational_and_backup_files(self):
        files = {"server/main.go": "package main", "server/go.mod": "module fixture",
                 "server/.env.prod": "secret", "server/credentials.json.bak": "secret",
                 "server/.state/credentials.json": "secret", "credentials.py": "secret",
                 "murmur/config.backup.json": "secret", "webapp/index.js": "fixture",
                 "README.md": "[Operations](OPERATIONS.md) and [server guide](server/README.md)",
                 "OPERATIONS.md": "[Server guide](server/README.md)",
                 "server/README.md": "[Main guide](../README.md)"}
        for name, data in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        directory, manifest = release.snapshot(self.source, self.root / "releases")
        selected = set(manifest["files"])
        self.assertIn("tools/murmur/server/main.go", selected)
        self.assertIn("tools/murmur/webapp/index.js", selected)
        self.assertIn("skills/murmur/SKILL.md", selected)
        for name, targets in {"README.md": ["OPERATIONS.md", "server/README.md"],
                              "OPERATIONS.md": ["server/README.md"], "server/README.md": ["../README.md"]}.items():
            document = directory / "tools/murmur" / name
            self.assertTrue(document.is_file(), name)
            for target in targets:
                self.assertIn("(" + target + ")", document.read_text())
                self.assertTrue((document.parent / target).is_file(), (name, target))
        self.assertFalse(any("credentials" in name or ".env" in name or "backup" in name for name in selected))
        target = directory / "tools/murmur/murmur/__init__.py"
        target.write_text("modified release")
        with self.assertRaisesRegex(ValueError, "modified"):
            release.snapshot(self.source, self.root / "releases")


if __name__ == "__main__":
    unittest.main()
