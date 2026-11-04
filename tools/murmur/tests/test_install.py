"""Installer fixtures never touch native settings or invoke host launchd."""

import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("murmur_installer", Path(__file__).parents[1] / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        # This class exercises the historical macOS contract on every host.
        # These cases isolate post-observation mutation/reload recovery. The
        # real observation boundary has its own MacObservationTests fixtures.
        preflight = patch.object(installer, "_mac_preflight")
        preflight.start()
        self.addCleanup(preflight.stop)
        platform = patch.object(installer.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="murmur-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.prefix = self.root / "home with 'spaces $()"
        self.state = self.prefix / "fresh state"
        self.source = self.root / "checkout with 'spaces $()" / "tools/murmur"
        (self.source / "murmur").mkdir(parents=True)
        (self.source / "murmur/__init__.py").write_text("")
        (self.source / "murmur/__main__.py").write_text(
            "import json,os,sys\n"
            "print(json.dumps({'arguments':sys.argv[1:], 'home':os.environ.get('MURMUR_HOME'),"
            "'input':sys.stdin.read() if 'hook' in sys.argv else None}))\n")
        self.skill = self.source.parents[1] / "skills/murmur"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("fixture skill")
        self.launches = []

    def install(self, **kwargs):
        return installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                 runner=self.record_launchctl, **kwargs)

    def record_launchctl(self, arguments, **kwargs):
        self.launches.append(arguments)
        if arguments[0] == "print":
            raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")

    def write_json(self, relative, value):
        path = self.prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def hooks(self, relative):
        return json.loads((self.prefix / relative).read_text())["hooks"]

    def command_bindings(self, relative, commands):
        """Foreign state belongs to this source/event/group/handler position."""
        path = self.prefix / relative
        return {(str(path), event, group_index, handler_index): handler["command"]
                for event, groups in self.hooks(relative).items()
                for group_index, group in enumerate(groups)
                for handler_index, handler in enumerate(group["hooks"])
                if handler.get("type") == "command" and handler.get("command") in commands}

    def installation_snapshot(self):
        result = {}
        for path in self.prefix.rglob("*"):
            mode = path.lstat().st_mode
            value = (("symlink", os.readlink(path)) if path.is_symlink() else
                     ("file", path.read_bytes()) if path.is_file() else ("directory",))
            result[str(path.relative_to(self.prefix))] = (mode, value)
        return result

    def test_install_and_remove_preserve_unrelated_settings_and_later_edits(self):
        foreign = {"type": "command", "command": "echo foreign", "timeout": 19}
        original = {"description": "existing", "hooks": {"Stop": [
            {"matcher": "foreign", "hooks": [foreign]}], "PostToolUse": [
            {"matcher": "shell", "hooks": [foreign], "description": "existing tool hook"}]},
            "unrelated": {"value": 42}}
        codex = self.write_json(".codex/hooks.json", original)
        claude = self.write_json(".claude/settings.json", {"model": "unchanged", "hooks": {}})
        toml = self.prefix / ".codex/config.toml"
        toml.write_bytes(b"[features]\nhooks = false\n# keep trust settings\n")
        result = self.install()
        self.assertFalse(result["active"])
        self.assertEqual(self.launches, [])
        self.assertEqual(toml.read_bytes(), b"[features]\nhooks = false\n# keep trust settings\n")
        value = json.loads(codex.read_text())
        self.assertEqual(value["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(value["hooks"]["PostToolUse"][0], original["hooks"]["PostToolUse"][0])
        # Foreign handlers before our removable suffix keep their positions.
        # Merely mentioning our marker never makes a command ours.
        later = {"type": "command", "command": "echo " + installer.MARKER}
        value["hooks"]["Stop"][-1]["hooks"].insert(0, later)
        value["later"] = "preserved"
        codex.write_text(json.dumps(value))
        result = installer.remove(self.prefix, self.state, runner=self.launches.append)
        self.assertTrue(result["removed"])
        after = json.loads(codex.read_text())
        self.assertEqual(after["unrelated"], original["unrelated"])
        self.assertEqual(after["later"], "preserved")
        self.assertEqual(after["hooks"], {"Stop": [original["hooks"]["Stop"][0], {"hooks": [later]}],
                                        "PostToolUse": original["hooks"]["PostToolUse"]})
        self.assertEqual(json.loads(claude.read_text()), {"model": "unchanged", "hooks": {}})
        self.assertFalse((self.prefix / ".local/bin/murmur").exists())
        self.assertTrue(self.state.exists())
        self.assertEqual(self.launches, [])

    def test_claude_peer_acceptance_default_preserves_absent_and_existing_policy(self):
        for policy in (None, "hold", "refuse", "accept"):
            with self.subTest(policy=policy):
                original = {"model": "user choice"}
                if policy is not None:
                    original["crossSessionInbound"] = policy
                path = self.write_json(".claude/settings.json", original)
                self.install()
                installed = json.loads(path.read_text())
                self.assertEqual({key: value for key, value in installed.items() if key != "hooks"}, original)
                installer.remove(self.prefix, self.state)
                self.assertEqual(json.loads(path.read_text()), original)

    def test_opt_in_claude_peer_acceptance_preserves_other_settings_and_reinstalls(self):
        foreign = {"type": "command", "command": "echo foreign", "timeout": 19}
        original = {"crossSessionInbound": "hold", "model": "user choice",
                    "hooks": {"Stop": [{"hooks": [foreign]}]}}
        path = self.write_json(".claude/settings.json", original)
        path.chmod(0o640)
        codex = self.write_json(".codex/hooks.json", {"crossSessionInbound": "refuse"})
        toml = self.prefix / ".codex/config.toml"
        toml.write_bytes(b"# unrelated native trust\n[features]\nhooks = false\n")
        trust = toml.read_bytes()

        install = installer.install
        def fixture_install(prefix, state, **options):
            return install(prefix, state, source=self.source, runner=self.record_launchctl, **options)
        with patch.object(installer, "install", side_effect=fixture_install), redirect_stdout(io.StringIO()):
            installer.main(["--prefix", str(self.prefix), "--state", str(self.state),
                            "--development-checkout", "--claude-accept-peer-messages"])
        self.assertEqual(json.loads(path.read_text())["crossSessionInbound"], "accept")
        self.assertEqual(json.loads(codex.read_text())["crossSessionInbound"], "refuse")
        self.install()
        self.assertEqual(json.loads(path.read_text())["crossSessionInbound"], "accept")
        updated = json.loads(path.read_text())
        updated.update(crossSessionInbound="hold", later="preserve")
        path.write_text(json.dumps(updated))
        self.install()
        self.assertEqual(json.loads(path.read_text())["crossSessionInbound"], "hold")
        self.install(claude_accept_peer_messages=True)
        installed = json.loads(path.read_text())
        self.assertEqual(installed["crossSessionInbound"], "accept")
        self.assertEqual(installed["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

        installer.remove(self.prefix, self.state)
        self.assertEqual(json.loads(path.read_text()),
                         {**original, "crossSessionInbound": "accept", "later": "preserve"})
        self.assertEqual(json.loads(codex.read_text()), {"crossSessionInbound": "refuse"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(toml.read_bytes(), trust)

    def test_opt_in_claude_peer_acceptance_retains_new_file_and_later_choice_on_remove(self):
        path = self.prefix / ".claude/settings.json"
        for policy in ("accept", "refuse"):
            with self.subTest(policy=policy):
                self.assertFalse(path.exists())
                self.install(claude_accept_peer_messages=True)
                updated = json.loads(path.read_text())
                updated["crossSessionInbound"] = policy
                path.write_text(json.dumps(updated))
                installer.remove(self.prefix, self.state)
                self.assertEqual(json.loads(path.read_text()), {"crossSessionInbound": policy})
                path.unlink()

    def test_opt_in_claude_peer_acceptance_preflight_and_failed_write_preserve_policy(self):
        path = self.write_json(".claude/settings.json", {"crossSessionInbound": "hold"})
        before = self.installation_snapshot()
        with patch.object(installer.Path, "home", return_value=self.prefix), \
                patch.object(installer, "_mac_preflight", side_effect=installer.InstallError("foreign service")):
            with self.assertRaisesRegex(installer.InstallError, "foreign service"):
                self.install(claude_accept_peer_messages=True)
        self.assertEqual(self.installation_snapshot(), before)

        atomic = installer._atomic
        def failed_write(target, content, mode=0o600, **options):
            if target == path:
                raise OSError("injected settings failure")
            return atomic(target, content, mode, **options)
        with patch.object(installer, "_atomic", side_effect=failed_write):
            with self.assertRaisesRegex(OSError, "injected settings failure"):
                self.install(claude_accept_peer_messages=True)
        self.assertEqual(json.loads(path.read_text()), {"crossSessionInbound": "hold"})

    def test_opt_in_claude_peer_acceptance_preserves_newer_policy_on_conflict(self):
        path = self.write_json(".claude/settings.json", {"crossSessionInbound": "hold"})
        atomic = installer._atomic
        def concurrent(target, content, mode=0o600, **options):
            if target == path:
                newer = json.loads(path.read_text())
                newer.update(crossSessionInbound="refuse", later="preserve")
                path.write_text(json.dumps(newer))
            return atomic(target, content, mode, **options)
        with patch.object(installer, "_atomic", side_effect=concurrent):
            with self.assertRaisesRegex(installer.InstallError, "newer settings preserved"):
                self.install(claude_accept_peer_messages=True)
        expected = {"crossSessionInbound": "refuse", "later": "preserve"}
        self.assertEqual(json.loads(path.read_text()), expected)
        installer.remove(self.prefix, self.state)
        self.assertEqual(json.loads(path.read_text()), expected)

    def test_remove_rejects_claude_peer_acceptance_option(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.object(installer, "remove") as remove:
            with self.assertRaises(SystemExit) as failure:
                installer.main(["--remove", "--claude-accept-peer-messages"])
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("--remove and --claude-accept-peer-messages are mutually exclusive", output.getvalue())
        remove.assert_not_called()

    def test_reinstall_keeps_later_and_mixed_foreign_hook_bindings(self):
        relatives = (".codex/hooks.json", ".claude/settings.json")
        foreign = {name: {"type": "command", "command": "echo foreign-" + name}
                   for name in ("tool", "before", "after", "later-group")}
        tool_group = {"matcher": "shell", "hooks": [foreign["tool"]]}
        self.write_json(relatives[0], {"description": "preserved", "hooks": {"SessionStart": [tool_group]}})
        old_codex_events = tuple(event for event in installer.EVENTS["codex"] if event != "SessionStart")
        with patch.dict(installer.EVENTS, {"codex": old_codex_events}):
            self.install()
        for relative in relatives:
            path = self.prefix / relative
            document = json.loads(path.read_text())
            group = document["hooks"]["Stop"][0]
            owned = group["hooks"][0]
            owned["timeout"] = 99
            group.update({"matcher": "*", "description": "mixed group metadata"})
            group["hooks"] = [foreign["before"], owned, foreign["after"]]
            document["hooks"]["Stop"].append({"matcher": "later", "hooks": [foreign["later-group"]]})
            path.write_text(json.dumps(document))
        commands = {handler["command"] for handler in foreign.values()}
        before = {relative: self.command_bindings(relative, commands) for relative in relatives}
        trust = self.prefix / ".codex/config.toml"
        trust.write_bytes(b"# positional trust and enable state must remain untouched\n[hooks.state]\n")
        self.install()
        for relative in relatives:
            self.assertEqual(self.command_bindings(relative, commands), before[relative])
            group = self.hooks(relative)["Stop"][0]
            self.assertEqual(group["matcher"], "*")
            self.assertEqual(group["description"], "mixed group metadata")
            self.assertEqual(group["hooks"][1]["timeout"], 8)
            self.assertEqual(len(self.hooks(relative)["Stop"]), 2)
        self.assertEqual(self.hooks(relatives[0])["SessionStart"][0], tool_group)
        self.assertEqual(len(self.hooks(relatives[0])["SessionStart"]), 2)
        self.assertEqual(trust.read_bytes(),
                         b"# positional trust and enable state must remain untouched\n[hooks.state]\n")

    def test_removal_keeps_empty_group_slots_and_foreign_handler_indexes(self):
        self.install()
        relatives = (".codex/hooks.json", ".claude/settings.json")
        foreign = {name: {"type": "command", "command": "echo foreign-" + name}
                   for name in ("later-group", "mixed")}
        for relative in relatives:
            path = self.prefix / relative
            document = json.loads(path.read_text())
            owned = document["hooks"]["Stop"][0]["hooks"][0]
            document["hooks"]["Stop"].extend([
                {"matcher": "later", "hooks": [foreign["later-group"]]},
                {"description": "preserved", "hooks": [foreign["mixed"], owned]}])
            path.write_text(json.dumps(document))
        commands = {handler["command"] for handler in foreign.values()}
        before = {relative: self.command_bindings(relative, commands) for relative in relatives}
        installer.remove(self.prefix, self.state)
        for relative in relatives:
            self.assertEqual(self.command_bindings(relative, commands), before[relative])
            self.assertEqual(self.hooks(relative), {"Stop": [
                {"hooks": []}, {"matcher": "later", "hooks": [foreign["later-group"]]},
                {"description": "preserved", "hooks": [foreign["mixed"]]}]})

    def test_impossible_mixed_removal_holds_entire_operation_before_any_mutation(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
        path = self.prefix / ".claude/settings.json"
        document = json.loads(path.read_text())
        document["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "echo foreign-after"})
        path.write_text(json.dumps(document))
        before = self.installation_snapshot()
        self.launches.clear()
        # Codex's settings have already been planned when the Claude hold occurs.
        # The reinstall case removes an event retired by a future installer.
        events = tuple(event for event in installer.EVENTS["claude"] if event != "Stop")
        for operation in ("remove", "reinstall"):
            with self.subTest(operation=operation), \
                    patch.object(installer.Path, "home", return_value=self.prefix), \
                    patch.dict(installer.EVENTS, {"claude": events}), \
                    patch.object(installer, "_atomic", side_effect=AssertionError("mutation before hold")):
                with self.assertRaisesRegex(installer.InstallError,
                                           "Cannot remove Murmur Stop group 0, handler 0"):
                    if operation == "remove":
                        installer.remove(self.prefix, self.state, runner=self.record_launchctl)
                    else:
                        self.install(activate=True)
            self.assertEqual(self.launches, [])
            self.assertEqual(self.installation_snapshot(), before)

    def test_inactive_plist_and_bounded_hooks_are_pinned_to_state(self):
        self.install()
        plist = plistlib.loads((self.prefix / "Library/LaunchAgents" / f"{installer.LABEL}.plist").read_bytes())
        self.assertTrue(plist["Disabled"])
        self.assertEqual(plist["ProgramArguments"], [str(self.prefix / ".local/bin/murmur"), "daemon"])
        self.assertEqual(plist["EnvironmentVariables"], {"MURMUR_HOME": str(self.state)})
        for provider, relative in (("codex", ".codex/hooks.json"), ("claude", ".claude/settings.json")):
            hooks = self.hooks(relative)
            expected = set(installer.EVENTS[provider])
            if provider == "claude":
                expected.remove("PostToolUse")
                self.assertNotIn("PostToolUse", hooks)
            else:
                self.assertIn("PostToolUse", hooks)
            self.assertEqual(set(hooks), expected)
            for event, groups in hooks.items():
                self.assertEqual(len(groups), 1)
                handler = groups[0]["hooks"][0]
                self.assertEqual(handler["timeout"], 3 if event in {"SessionEnd", "Interrupt"} else 8)
                process = subprocess.run(handler["command"], shell=True, executable="/bin/sh",
                                         input='{"fixture":true}', capture_output=True, text=True, check=True,
                                         env={**os.environ, "MURMUR_HOME": str(self.root / "wrong state")})
                returned = json.loads(process.stdout)
                self.assertEqual(returned, {"arguments": ["hook", provider, event],
                                            "home": str(self.state), "input": '{"fixture":true}'})
        self.assertTrue((self.prefix / ".codex/skills/murmur").is_symlink())
        self.assertEqual((self.prefix / ".claude/skills/murmur").resolve(), self.skill)

    def test_launcher_uses_captured_python_from_other_cwd_and_keeps_arguments(self):
        self.install()
        launcher = self.prefix / ".local/bin/murmur"
        arguments = ["send", "opaque-chat", "' $() `literal` text"]
        environment = dict(os.environ)
        environment.pop("MURMUR_HOME", None)
        process = subprocess.run([str(launcher), *arguments], cwd=self.root,
                                 env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(process.stdout), {"arguments": arguments,
                                                     "home": str(self.state), "input": None})
        override = self.root / "explicit caller state"
        process = subprocess.run([str(launcher), "inbox"], cwd="/", capture_output=True,
                                 env={**environment, "MURMUR_HOME": str(override)}, text=True, check=True)
        self.assertEqual(json.loads(process.stdout)["home"], str(override))

    def test_reinstallation_is_idempotent_and_removal_restores_missing_files(self):
        self.install()
        self.install()
        self.assertEqual(len(self.hooks(".codex/hooks.json")["Stop"]), 1)
        self.assertEqual(len(self.hooks(".claude/settings.json")["SessionStart"]), 1)
        installer.remove(self.prefix, self.state)
        self.assertFalse((self.prefix / ".codex/hooks.json").exists())
        self.assertFalse((self.prefix / ".claude/settings.json").exists())
        self.assertFalse((self.state / "installation.json").exists())

    def test_foreign_artifact_collisions_are_preserved_before_any_mutations(self):
        for relative, symlink in ((".local/bin/murmur", False),
                                  (".codex/skills/murmur", True),
                                  (f"Library/LaunchAgents/{installer.LABEL}.plist", False)):
            with self.subTest(relative=relative):
                path = self.prefix / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if symlink:
                    path.symlink_to(self.root / "foreign-skill")
                else:
                    path.write_text("foreign integration")
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertFalse(self.state.exists())
                if symlink:
                    self.assertEqual(os.readlink(path), str(self.root / "foreign-skill"))
                else:
                    self.assertEqual(path.read_text(), "foreign integration")
                path.unlink()

    def test_invalid_or_symlink_settings_leave_everything_untouched(self):
        path = self.prefix / ".claude/settings.json"
        path.parent.mkdir(parents=True)
        for content in ("{broken", "[]", '{"hooks":{"Stop":"invalid"}}'):
            path.write_text(content)
            with self.assertRaises(installer.InstallError):
                self.install()
            self.assertEqual(path.read_text(), content)
            self.assertFalse((self.prefix / ".local/bin/murmur").exists())
            self.assertFalse(self.state.exists())
        path.unlink()
        foreign = self.root / "managed-settings.json"
        foreign.write_text('{}')
        path.symlink_to(foreign)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(foreign.read_text(), '{}')

    def test_modified_owned_files_are_preserved_and_settings_are_removed(self):
        self.install()
        launcher = self.prefix / ".local/bin/murmur"
        launcher.write_text("modified launcher")
        with self.assertRaises(installer.InstallError):
            self.install()
        result = installer.remove(self.prefix, self.state)
        self.assertIn(str(launcher), result["preserved"])
        self.assertEqual(launcher.read_text(), "modified launcher")
        self.assertFalse((self.prefix / ".codex/hooks.json").exists())

    def test_fixture_activation_is_rejected_and_host_activation_can_be_faked(self):
        with self.assertRaises(installer.InstallError):
            self.install(activate=True)
        self.assertFalse(self.state.exists())
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            self.assertEqual([call[0] for call in self.launches], ["enable", "bootstrap", "kickstart"])
            installer.remove(self.prefix, self.state, runner=self.launches.append)
            self.assertEqual(self.launches[-1], ["bootout", f"gui/{os.getuid()}/{installer.LABEL}"])

    def test_removal_restores_original_absent_hooks_and_empty_event(self):
        first = {"model": "preserved"}
        second = {"hooks": {"Stop": []}, "description": "preserved"}
        self.write_json(".claude/settings.json", first)
        self.write_json(".codex/hooks.json", second)
        self.install()
        self.install()
        installer.remove(self.prefix, self.state)
        self.assertEqual(json.loads((self.prefix / ".claude/settings.json").read_text()), first)
        self.assertEqual(json.loads((self.prefix / ".codex/hooks.json").read_text()), second)

    def test_failed_changing_reinstall_retains_ownership_and_retries(self):
        self.install()
        launcher = self.prefix / ".local/bin/murmur"
        original = launcher.read_bytes()
        alternate = self.root / "alternate-python"
        alternate.write_text("#!/bin/sh\nexit 0\n")
        alternate.chmod(0o755)
        atomic = installer._atomic

        def fail_launcher(path, content, mode=0o600, **kwargs):
            if path == launcher:
                raise OSError("injected replacement failure")
            return atomic(path, content, mode, **kwargs)

        with patch.object(installer, "_atomic", side_effect=fail_launcher):
            with self.assertRaisesRegex(OSError, "injected"):
                self.install(python=alternate)
        self.assertEqual(launcher.read_bytes(), original)
        self.install(python=alternate)
        self.assertNotEqual(launcher.read_bytes(), original)
        result = installer.remove(self.prefix, self.state)
        self.assertEqual(result["preserved"], [])

    def test_failed_activation_retries_bootstrap_and_absent_job_can_be_removed(self):
        def fail_bootstrap(arguments, **kwargs):
            self.launches.append(arguments)
            if arguments[0] in {"bootstrap", "print"}:
                raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")

        with patch.object(installer.Path, "home", return_value=self.prefix):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                  activate=True, runner=fail_bootstrap)
            manifest = json.loads((self.state / "installation.json").read_text())
            self.assertFalse(manifest["active"])
            self.assertTrue(manifest["activation_pending"])
            # The next activation must try bootstrap again, not merely kickstart.
            self.launches.clear()
            self.install(activate=True)
            self.assertEqual([call[0] for call in self.launches], ["enable", "bootstrap", "kickstart"])

            def absent_job(arguments, **kwargs):
                raise subprocess.CalledProcessError(3, arguments, stderr="Boot-out failed: 3: No such process")

            result = installer.remove(self.prefix, self.state, runner=absent_job)
            self.assertTrue(result["removed"])

    def test_removal_retries_after_bootout_and_does_not_ignore_permission_error(self):
        self.write_json(".codex/hooks.json", {"description": "foreign"})
        atomic = installer._atomic
        codex = self.prefix / ".codex/hooks.json"

        def fail_settings(path, content, mode=0o600, **kwargs):
            if path == codex:
                raise OSError("injected settings failure")
            return atomic(path, content, mode, **kwargs)

        def denied(arguments, **kwargs):
            raise subprocess.CalledProcessError(5, arguments, stderr="Operation not permitted")

        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            with self.assertRaises(subprocess.CalledProcessError):
                installer.remove(self.prefix, self.state, runner=denied)
            self.assertTrue(json.loads((self.state / "installation.json").read_text())["active"])
            self.launches.clear()
            with patch.object(installer, "_atomic", side_effect=fail_settings):
                with self.assertRaisesRegex(OSError, "injected"):
                    installer.remove(self.prefix, self.state, runner=self.launches.append)
            self.assertEqual([call[0] for call in self.launches], ["bootout"])
            self.launches.clear()
            self.assertTrue(installer.remove(self.prefix, self.state, runner=self.launches.append)["removed"])
            self.assertEqual(self.launches, [])

    def test_active_upgrade_reloads_the_changed_loaded_definition(self):
        loaded = {"definition": None, "generation": 0}
        original_dumps = plistlib.dumps

        def old_version_plist(value):
            value = {**value, "EnvironmentVariables": {
                **value["EnvironmentVariables"], "MURMUR_RUNTIME_VERSION": "before-upgrade"}}
            return original_dumps(value)

        def launchctl(arguments, **kwargs):
            self.launches.append(arguments)
            if arguments[0] == "bootout":
                loaded["definition"] = None
            elif arguments[0] == "print" and loaded["definition"] is None:
                raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")
            elif arguments[0] == "bootstrap":
                if loaded["definition"] is not None:
                    raise subprocess.CalledProcessError(5, arguments, stderr="Service already loaded")
                loaded["definition"] = plistlib.loads(Path(arguments[2]).read_bytes())
                loaded["generation"] += 1
            elif arguments[0] == "kickstart":
                self.assertIsNotNone(loaded["definition"])

        with patch.object(installer.Path, "home", return_value=self.prefix):
            with patch.object(installer.plistlib, "dumps", side_effect=old_version_plist):
                installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                  activate=True, runner=launchctl)
            self.assertEqual(loaded["definition"]["EnvironmentVariables"]["MURMUR_RUNTIME_VERSION"],
                             "before-upgrade")
            self.launches.clear()
            result = installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                       activate=True, runner=launchctl)
        self.assertEqual([call[0] for call in self.launches],
                         ["bootout", "print", "enable", "bootstrap", "kickstart"])
        self.assertEqual(loaded["generation"], 2)
        self.assertEqual(loaded["definition"]["EnvironmentVariables"], {"MURMUR_HOME": str(self.state)})
        self.assertTrue(result["active"])
        self.assertFalse(json.loads((self.state / "installation.json").read_text())["activation_pending"])

    def test_refresh_permission_failures_remain_failures_with_recoverable_ownership(self):
        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            for failing_stage in ("bootout", "bootstrap"):
                with self.subTest(stage=failing_stage):
                    self.launches.clear()

                    def denied(arguments, **kwargs):
                        self.launches.append(arguments)
                        if arguments[0] == failing_stage:
                            raise subprocess.CalledProcessError(5, arguments, stderr="Operation not permitted")
                        if arguments[0] == "print":
                            raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")
                        # A superficial `print` would succeed. It must never
                        # turn the permission error into activation success.

                    with self.assertRaises(subprocess.CalledProcessError):
                        installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                          activate=True, runner=denied)
                    self.assertEqual([call[0] for call in self.launches],
                                     ["bootout"] if failing_stage == "bootout" else
                                     ["bootout", "print", "enable", "bootstrap", "print"])
                    manifest = json.loads((self.state / "installation.json").read_text())
                    self.assertTrue(manifest["activation_pending"])
                    self.assertEqual(manifest["active"], failing_stage == "bootout")
                    self.assertEqual(manifest["job_owned"], failing_stage == "bootout")
            result = installer.remove(self.prefix, self.state, runner=self.launches.append)
            self.assertTrue(result["removed"])
            self.assertEqual(result["preserved"], [])

    def test_failed_bootstrap_never_authorizes_stopping_an_unknown_job(self):
        actions = []
        def foreign_job(arguments, **kwargs):
            actions.append(arguments[0])
            if arguments[0] == "bootstrap":
                raise subprocess.CalledProcessError(5, arguments, stderr="Service already loaded")
            if arguments[0] == "print":
                return subprocess.CompletedProcess(arguments, 0, stdout="unverified present service")
            if arguments[0] == "bootout":
                self.fail("An unrelated loaded service must not be stopped")
        with patch.object(installer.Path, "home", return_value=self.prefix):
            with self.assertRaisesRegex(installer.InstallError, "ownership"):
                installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                  activate=True, runner=foreign_job)
            before = (self.state / "installation.json").read_bytes()
            for action in (lambda: self.install(activate=True),
                           lambda: installer.remove(self.prefix, self.state, runner=foreign_job)):
                with self.assertRaisesRegex(installer.InstallError, "outcome is uncertain"):
                    action()
            self.assertEqual(actions, ["enable", "bootstrap", "print"])
            self.assertEqual((self.state / "installation.json").read_bytes(), before)

    def test_explicit_fixture_prefix_ignores_ambient_runtime_home(self):
        ambient = self.root / "ambient-runtime"
        ambient.mkdir()
        sentinel = ambient / "installation.json"
        sentinel.write_bytes(b"untouched runtime sentinel")
        fixture_state = self.prefix / ".local/state/murmur-mattermost"
        with patch.dict(os.environ, {"MURMUR_HOME": str(ambient)}):
            result = installer.install(self.prefix, source=self.source, development_checkout=True, runner=self.launches.append)
            self.assertEqual(result["state"], str(fixture_state))
            self.assertTrue((fixture_state / "installation.json").is_file())
            self.assertEqual(sentinel.read_bytes(), b"untouched runtime sentinel")
            result = installer.remove(self.prefix, runner=self.launches.append)
        self.assertTrue(result["removed"])
        self.assertEqual(list(ambient.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"untouched runtime sentinel")
        self.assertEqual(self.launches, [])

    def test_post_bootstrap_journal_failure_stops_the_known_owned_job(self):
        atomic = installer._atomic
        running = {"value": False}

        def launchctl(arguments, **kwargs):
            self.launches.append(arguments)
            if arguments[0] == "bootstrap":
                running["value"] = True
            elif arguments[0] == "bootout":
                running["value"] = False

        def fail_owned_journal(path, content, mode=0o600, **kwargs):
            if path.name == "installation.json" and json.loads(content).get("job_owned"):
                raise OSError("injected ownership journal failure")
            return atomic(path, content, mode, **kwargs)

        with patch.object(installer.Path, "home", return_value=self.prefix):
            with patch.object(installer, "_atomic", side_effect=fail_owned_journal):
                with self.assertRaisesRegex(OSError, "ownership journal"):
                    installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                      activate=True, runner=launchctl)
            self.assertFalse(running["value"])
            self.assertEqual([call[0] for call in self.launches], ["enable", "bootstrap", "bootout"])
            self.assertTrue(installer.remove(self.prefix, self.state, runner=launchctl)["removed"])
            self.assertFalse(running["value"])

    def test_unknown_bootstrap_outcome_holds_retry_and_removal(self):
        def uncertain(arguments, **kwargs):
            self.launches.append(arguments)
            if arguments[0] == "bootstrap":
                raise subprocess.TimeoutExpired(arguments, 10)

        with patch.object(installer.Path, "home", return_value=self.prefix):
            with self.assertRaises(subprocess.TimeoutExpired):
                installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                  activate=True, runner=uncertain)
            before = (self.state / "installation.json").read_bytes()
            self.launches.clear()
            with self.assertRaisesRegex(installer.InstallError, "outcome is uncertain"):
                self.install(activate=True)
            with self.assertRaisesRegex(installer.InstallError, "outcome is uncertain"):
                installer.remove(self.prefix, self.state, runner=self.launches.append)
            self.assertEqual(self.launches, [])
            self.assertEqual((self.state / "installation.json").read_bytes(), before)
            self.assertTrue((self.prefix / ".local/bin/murmur").is_file())

    def test_owned_reload_retries_transient_eio_only_after_confirmed_absence(self):
        calls = []
        attempts = 0

        def launchctl(arguments, **kwargs):
            nonlocal attempts
            calls.append(arguments)
            if arguments[0] == "bootstrap":
                attempts += 1
                if attempts == 1:
                    raise subprocess.CalledProcessError(5, arguments, stderr="Bootstrap failed: 5: Input/output error")
            if arguments[0] == "print":
                raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")

        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            with patch.object(installer.time, "sleep") as sleep:
                result = installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                           activate=True, runner=launchctl)
        self.assertTrue(result["active"])
        self.assertEqual([call[0] for call in calls],
                         ["bootout", "print", "enable", "bootstrap", "print", "bootstrap", "kickstart"])
        self.assertEqual(calls[4], ["print", f"gui/{os.getuid()}/{installer.LABEL}"])
        sleep.assert_called_once_with(0.1)

    def test_owned_reload_retry_exhaustion_is_bounded_and_retains_recoverable_artifacts(self):
        calls = []

        def launchctl(arguments, **kwargs):
            calls.append(arguments[0])
            if arguments[0] == "bootstrap":
                raise subprocess.CalledProcessError(5, arguments, stderr="Bootstrap failed: 5: Input/output error")
            if arguments[0] == "print":
                raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")

        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            with patch.object(installer.time, "sleep") as sleep:
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                      activate=True, runner=launchctl)
        self.assertIn("Input/output error", failure.exception.stderr)
        self.assertEqual(calls, ["bootout", "print", "enable", "bootstrap", "print",
                                 "bootstrap", "print", "bootstrap", "print", "print"])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.1, 0.2])
        manifest = json.loads((self.state / "installation.json").read_text())
        self.assertFalse(manifest["active"])
        self.assertFalse(manifest["job_owned"])
        self.assertFalse(manifest["bootstrap_inflight"])
        self.assertTrue(manifest["activation_pending"])
        self.assertTrue((self.prefix / ".local/bin/murmur").is_file())

    def test_absent_old_job_does_not_authorize_owned_reload_retry(self):
        calls = []

        def launchctl(arguments, **kwargs):
            calls.append(arguments[0])
            if arguments[0] == "bootout":
                raise subprocess.CalledProcessError(3, arguments, stderr="No such process")
            if arguments[0] == "bootstrap":
                raise subprocess.CalledProcessError(5, arguments, stderr="Input/output error")
            if arguments[0] == "print":
                raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")

        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            with patch.object(installer.time, "sleep") as sleep:
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                      activate=True, runner=launchctl)
        self.assertEqual(calls, ["bootout", "print", "enable", "bootstrap", "print"])
        sleep.assert_not_called()

    def test_reload_refuses_present_permission_timeout_and_ambiguous_absence_probes(self):
        for outcome in ("present", "permission", "timeout", "ambiguous"):
            with self.subTest(outcome=outcome):
                self.prefix = self.root / ("home-" + outcome)
                self.state = self.prefix / "fresh-state"
                calls = []
                probes = 0

                def launchctl(arguments, **kwargs):
                    nonlocal probes
                    calls.append(arguments[0])
                    if arguments[0] == "bootstrap":
                        raise subprocess.CalledProcessError(5, arguments, stderr="Original bootstrap Input/output error")
                    if arguments[0] == "print":
                        probes += 1
                        if probes == 1:
                            raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")
                        if outcome == "permission":
                            raise subprocess.CalledProcessError(5, arguments, stderr="Permission denied")
                        if outcome == "timeout":
                            raise subprocess.TimeoutExpired(arguments, 10)
                        if outcome == "ambiguous":
                            raise subprocess.CalledProcessError(113, arguments, stderr="Unrecognized service error")

                with patch.object(installer.Path, "home", return_value=self.prefix):
                    self.install(activate=True)
                    with patch.object(installer.time, "sleep") as sleep:
                        with self.assertRaisesRegex(installer.InstallError, "Original bootstrap Input/output error"):
                            installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                              activate=True, runner=launchctl)
                self.assertEqual(calls, ["bootout", "print", "enable", "bootstrap", "print"])
                sleep.assert_not_called()
                manifest = json.loads((self.state / "installation.json").read_text())
                self.assertTrue(manifest["bootstrap_inflight"])
                self.assertFalse(manifest["job_owned"])
                self.assertFalse(manifest["active"])

    def test_bootstrap_permission_reported_on_stdout_is_not_retried(self):
        calls = []

        def launchctl(arguments, **kwargs):
            calls.append(arguments[0])
            raise subprocess.CalledProcessError(5, arguments, output="Permission denied", stderr="")

        with patch.object(installer.time, "sleep") as sleep:
            with self.assertRaises(subprocess.CalledProcessError):
                installer._bootstrap(launchctl, self.prefix / "fixture.plist", stopped_owned=True)
        self.assertEqual(calls, ["bootstrap"])
        sleep.assert_not_called()

    def test_cli_failure_includes_captured_launchctl_stderr(self):
        output = io.StringIO()
        error = subprocess.CalledProcessError(5, ["launchctl", "bootstrap"], stderr="Specific launchd failure")
        with patch.object(installer, "install", side_effect=error), redirect_stderr(output):
            with self.assertRaises(SystemExit) as failure:
                installer.main(["--prefix", str(self.prefix)])
        self.assertEqual(failure.exception.code, 1)
        self.assertIn("Specific launchd failure", output.getvalue())

    def test_owned_reload_waits_for_old_service_to_disappear_before_bootstrap(self):
        clock = [0.0]
        probes = 0
        calls = []

        def launchctl(arguments, **kwargs):
            nonlocal probes
            calls.append(arguments[0])
            if arguments[0] == "print":
                probes += 1
                self.assertGreater(kwargs["timeout"], 0)
                self.assertLessEqual(kwargs["timeout"], 2)
                value = json.loads((self.state / "installation.json").read_text())
                self.assertFalse(value["bootstrap_inflight"])
                self.assertTrue(value["job_owned"])
                if probes == 3:
                    raise subprocess.CalledProcessError(113, arguments, stderr="Could not find service")
            elif arguments[0] == "bootstrap":
                self.assertEqual(probes, 3, "The old service must be absent before bootstrap")

        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            with patch.object(installer.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(installer.time, "sleep", side_effect=lambda duration: clock.__setitem__(0, clock[0] + duration)):
                result = installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                           activate=True, runner=launchctl)
        self.assertTrue(result["active"])
        self.assertEqual(calls, ["bootout", "print", "print", "print", "enable", "bootstrap", "kickstart"])
        self.assertAlmostEqual(clock[0], 0.2)

    def test_still_exiting_owned_service_times_out_before_bootstrap_and_can_retry(self):
        clock = [0.0]
        calls = []

        def still_exiting(arguments, **kwargs):
            calls.append(arguments[0])
            if arguments[0] == "print":
                self.assertGreater(kwargs["timeout"], 0)
                self.assertLessEqual(kwargs["timeout"], 2)

        with patch.object(installer.Path, "home", return_value=self.prefix):
            self.install(activate=True)
            with patch.object(installer.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(installer.time, "sleep", side_effect=lambda duration: clock.__setitem__(0, clock[0] + duration)):
                with self.assertRaisesRegex(installer.InstallError, "still exiting"):
                    installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                      activate=True, runner=still_exiting)
            value = json.loads((self.state / "installation.json").read_text())
            self.assertFalse(value["bootstrap_inflight"])
            self.assertTrue(value["job_owned"])
            self.assertTrue(value["activation_pending"])
            self.assertNotIn("bootstrap", calls)
            self.assertNotIn("enable", calls)
            self.assertAlmostEqual(clock[0], 2.0)
            self.assertTrue(self.install(activate=True)["active"])

    def test_prebootstrap_probe_errors_never_start_a_new_bootstrap(self):
        for outcome in ("permission", "timeout", "ambiguous"):
            with self.subTest(outcome=outcome):
                self.prefix = self.root / ("prebootstrap-" + outcome)
                self.state = self.prefix / "fresh-state"
                calls = []

                def launchctl(arguments, **kwargs):
                    calls.append(arguments[0])
                    if arguments[0] == "print":
                        self.assertLessEqual(kwargs["timeout"], 2)
                        if outcome == "timeout":
                            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
                        raise subprocess.CalledProcessError(5 if outcome == "permission" else 113,
                            arguments, stderr="Permission denied" if outcome == "permission" else "Unknown result")

                with patch.object(installer.Path, "home", return_value=self.prefix):
                    self.install(activate=True)
                    with self.assertRaises(installer.InstallError):
                        installer.install(self.prefix, self.state, source=self.source, development_checkout=True,
                                          activate=True, runner=launchctl)
                self.assertEqual(calls, ["bootout", "print"])
                value = json.loads((self.state / "installation.json").read_text())
                self.assertFalse(value["bootstrap_inflight"])
                self.assertTrue(value["job_owned"])

    def test_absence_deadline_is_passed_to_the_actual_subprocess_boundary(self):
        with patch.object(installer.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 2)) as run:
            with self.assertRaisesRegex(installer.InstallError, "absence probe timed out"):
                installer._wait_absent(installer._launchctl)
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
