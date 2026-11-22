"""Desktop wake boundaries with synthetic files and process/launcher facts only."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import struct
import tempfile
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from murmur.native import desktop_wake
from murmur.native.types import NativeError, NativeIdentity


SESSION = "11111111-1111-4111-8111-111111111111"
REAL_PEER_OWNER = desktop_wake._peer_owner
# Historical and arbitrary future labels are fixture metadata, never a runtime registry.
FIXTURE_PROFILES = tuple({"bundle_id": "com.openai.codex", "version": version, "build": build}
    for version, build in (("26.1103.32239", "25471"), ("26.1103.60933", "25894"),
                           ("26.1106.51428", "26605"), ("26.1111.21308", "27194"),
                           ("26.1111.71704", "29030"))) + ({
    "bundle_id": "com.openai.codex", "version": "26.1117.31558", "build": "29513",
    "helper_relative_path": "Contents/Frameworks/Codex Framework.framework/Versions/158.0.8127.37/"
                            "Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)",
    "launch_cwd_args": True},)



class DesktopWakeFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dw-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / ".codex").mkdir()
        self.bundle = self.root / "ChatGPT.app"
        self.app = self.bundle / "Contents/MacOS/ChatGPT"
        self.owner = self.bundle / desktop_wake._OWNER_RELATIVE_PATH
        self.launcher = self.bundle / "Contents/Resources/codex-cli/bin/codex"
        self.asar = self.bundle / "Contents/Resources/app.asar"
        for path, body in ((self.app, b"synthetic Desktop executable"),
                           (self.owner, b"synthetic native backend"),
                           (self.launcher, b"synthetic bundled queue launcher"),
                           (self.asar, b"synthetic supported wake source")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.profiles = tuple({**profile, "app_sha256": self.digest(self.app),
                               "owner_sha256": self.digest(self.owner),
                               "asar_sha256": self.digest(self.asar),
                               "launcher_sha256": self.digest(self.launcher)}
                              for profile in FIXTURE_PROFILES if not profile.get("helper_relative_path"))
        profile = self.profiles[-1]
        self.info = self.bundle / "Contents/Info.plist"
        self.metadata = {"CFBundleIdentifier": profile["bundle_id"],
                         "CFBundleShortVersionString": profile["version"],
                         "CFBundleVersion": profile["build"], "CFBundleExecutable": "ChatGPT"}
        self.info.write_bytes(plistlib.dumps(self.metadata))
        self.pipe = self.root / "p.sock"
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.pipe))
        self.pipe.chmod(0o600)
        uid = os.getuid()
        self.identities = {101: (201, uid, "tool birth"),
                           201: (301, uid, "owner birth"), 301: (1, uid, "app birth")}
        worker = str(self.root / "worker")
        self.commands = {
            101: (worker, (worker,)),
            201: (str(self.owner), (str(self.owner), "-c", "features.code_mode_host=true",
                                    "app-server", "--analytics-default-enabled")),
            301: (str(self.app), (str(self.app),)),
        }
        self.env = {"HOME": str(self.root), "CODEX_APP_TOOLS_PIPE_PATH": str(self.pipe)}
        patches = (
            patch.object(desktop_wake.sys, "platform", "darwin"),
            patch.object(desktop_wake.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.root))),
            patch.object(desktop_wake.os, "getppid", return_value=101),
            patch.object(desktop_wake.process, "identity", side_effect=lambda pid, **_: self.identities[pid]),
            patch.object(desktop_wake.process, "inspect", side_effect=lambda pid: self.commands[pid]),
            patch.object(desktop_wake.subprocess, "run", side_effect=self.run_command),
        )
        mocks = []
        for item in patches:
            mocks.append(item.start())
            self.addCleanup(item.stop)
        self.run = mocks[-1]
        self.peer_patch = patch.object(desktop_wake, '_peer_owner')
        self.peer_check = self.peer_patch.start()
        self.addCleanup(self.peer_patch.stop)

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run_command(self, command, **options):
        if command[0] == "/usr/sbin/lsof":
            self.assertIn("301", command)
            return subprocess.CompletedProcess(command, 0, stdout=f"p301\ncChatGPT\nf4\ntunix\nn{self.pipe}\n")
        if command[0] == "/usr/bin/open":
            return subprocess.CompletedProcess(command, 0)
        self.fail("Unexpected external command: " + repr(command))

    def identity(self, descriptor=None):
        return NativeIdentity("codex", SESSION, target={
            "binary": str(self.owner), "cwd": str(self.root), "transport": "queue", "cli_args": [],
            "env": {"HOME": str(self.root), "CODEX_HOME": None, "CODEX_SQLITE_HOME": None}},
            evidence={"desktop_wake": descriptor if descriptor is not None else desktop_wake.capture(self.env)})

    def successor_fixture(self):
        identity = self.identity()
        uid = os.getuid()
        self.identities.update({401: (501, uid, 'successor owner'), 501: (1, uid, 'successor app')})
        self.commands.update({401: self.commands[201], 501: self.commands[301]})
        self.successor_pipe_root = self.root / 'pipes'
        self.successor_pipe_root.mkdir()
        self.successor_pipe = self.successor_pipe_root / (str(uuid.uuid4()) + '.sock')
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.successor_pipe))
        self.successor_pipe.chmod(0o600)
        self.database_paths = [self.root / '.codex' / name for name in (
            'queue_1.sqlite', 'state_5.sqlite', 'thread_history_1.sqlite')]
        for path in self.database_paths:
            path.write_bytes(b'synthetic native database file')
            path.chmod(0o644)
        self.inventory = f'501 {uid} {self.app}\n401 {uid} {self.owner}\n'
        self.owner_files = '\n'.join(f'f{index}\nD{hex(path.stat().st_dev)}\ni{path.stat().st_ino}\nn{path}'
                                     for index, path in enumerate(self.database_paths))
        self.app_sockets = 'n' + str(self.successor_pipe)
        def command(argv, **options):
            if argv[:2] == ['/bin/ps', '-axo']:
                return subprocess.CompletedProcess(argv, 0, stdout=self.inventory)
            if argv[0] == '/usr/sbin/lsof':
                if '501' in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout=self.app_sockets)
                if '401' in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout=self.owner_files)
            self.fail('successor discovery attempted a write or unsupported command: ' + repr(argv))
        self.run.side_effect = command
        pipe_patch = patch.object(desktop_wake, '_TOOLS_DIRECTORY', str(self.successor_pipe_root))
        pipe_patch.start()
        self.addCleanup(pipe_patch.stop)
        return identity


class DesktopWakeTests(DesktopWakeFixture):
    def test_capture_is_deterministic_without_hashing_app_rpc_or_external_commands(self):
        with patch.object(desktop_wake.files, "executable_sha256", side_effect=AssertionError("capture hashed a file")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("capture connected a socket")):
            first, second = desktop_wake.capture(self.env), desktop_wake.capture(self.env)
        self.assertEqual(first, second)
        self.assertEqual((first["owner"]["pid"], first["owner"]["parent_pid"], first["app"]["pid"]),
                         (201, 301, 301))
        self.assertEqual(first["pipe"]["path"], str(self.pipe))
        self.assertTrue(first["domain"]["supported"])
        self.run.assert_not_called()

    def test_legacy_direct_capture_and_profile_keep_their_frozen_shape(self):
        captured = desktop_wake.capture(self.env)
        self.assertEqual(set(captured), {"schema", "kind", "app_bundle", "app", "owner", "pipe",
                                         "domain", "startup_supported"})
        self.assertEqual(set(captured["owner"]), {"pid", "parent_pid", "uid", "birth",
                                                 "executable", "argv_sha256"})
        verified = desktop_wake.verify(self.identity(captured))
        self.assertEqual(set(verified["verification"]["profile"]),
                         {"bundle_id", "version", "build", "app_sha256", "asar_sha256",
                          "owner_sha256", "launcher_sha256"})

    def test_actual_parentage_selects_direct_or_helper_independently_of_build(self):
        profile = next(p for p in self.profiles if p["build"] == "29030")
        self.info.write_bytes(plistlib.dumps({**self.metadata,
            "CFBundleShortVersionString": profile["version"], "CFBundleVersion": profile["build"]}))
        helper = (self.bundle / "Contents/Frameworks/Codex Framework.framework/Versions/157.0.8103.78"
                  / "Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)")
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes(b"bundled helper outside the captured backend chain")
        captured = desktop_wake.capture(self.env)
        self.assertNotIn("helper", captured)
        self.assertEqual(captured["owner"]["parent_pid"], captured["app"]["pid"])
        self.assertTrue(captured["startup_supported"])
        verified = desktop_wake.verify(self.identity(captured))
        self.assertEqual(verified["verification"]["profile"], profile)
        self.assertFalse({"helper_relative_path", "helper_sha256", "launch_cwd_args"} & set(profile))
        self.assertNotIn(str(helper), verified["verification"]["files"])
        self.identities[201] = (251, os.getuid(), "owner birth")
        self.identities[251] = (301, os.getuid(), "utility helper")
        self.commands[251] = (str(helper), (str(helper), "--type=utility",
                                           "--utility-sub-type=node.mojom.NodeService",
                                           "--service-sandbox-type=none"))
        self.run.reset_mock()
        helper_capture = desktop_wake.capture(self.env)
        self.assertEqual(helper_capture["helper"]["pid"], 251)
        self.assertEqual(desktop_wake.verify(self.identity(helper_capture))["verification"]["profile"]["helper_relative_path"],
                         str(helper.relative_to(self.bundle)))

    def test_owner_state_requires_positive_backend_end_not_inspection_failure_or_app_only_death(self):
        identity = self.identity()
        with patch.object(desktop_wake.os, 'kill', return_value=None):
            self.assertEqual(desktop_wake.owner_process_state(identity)['status'], 'alive')
            self.identities[301] = (1, os.getuid(), 'new app birth')
            state = desktop_wake.owner_process_state(identity)
            self.assertEqual(state['status'], 'unknown')
            self.assertEqual(state['components']['app']['status'], 'ended')
            self.identities[201] = (301, os.getuid(), 'new backend birth')
            self.assertEqual(desktop_wake.owner_process_state(identity)['status'], 'ended')
        with patch.object(desktop_wake.os, 'kill', side_effect=PermissionError):
            self.assertEqual(desktop_wake.owner_process_state(identity)['status'], 'unknown')
        with patch.object(desktop_wake.os, 'kill', side_effect=ProcessLookupError(3, 'missing')):
            state = desktop_wake.owner_process_state(identity)
            self.assertEqual(state['status'], 'ended')
            self.assertTrue(all(x['status'] == 'ended' for x in state['components'].values()))

    def test_owner_state_missing_evidence_and_failed_process_inspection_stay_unknown(self):
        self.assertEqual(desktop_wake.owner_process_state(NativeIdentity('codex', SESSION))['status'], 'unknown')
        identity = self.identity()
        with patch.object(desktop_wake.os, 'kill', return_value=None), \
                patch.object(desktop_wake.process, 'identity', side_effect=NativeError('unavailable')):
            self.assertEqual(desktop_wake.owner_process_state(identity)['status'], 'unknown')

    def test_successor_discovery_preserves_actor_target_and_verifies_owned_default_databases(self):
        identity = self.successor_fixture()
        original = deepcopy(identity)
        successor = desktop_wake.discover_successor(identity)
        self.assertEqual(identity, original)
        self.assertEqual(successor.target, identity.target)
        self.assertEqual(successor.session_id, identity.session_id)
        value = successor.evidence['desktop_wake']
        self.assertEqual((value['owner']['pid'], value['app']['pid']), (401, 501))
        self.assertEqual(value['pipe']['path'], str(self.successor_pipe))
        self.assertEqual(set(value['database_files']), {str(p) for p in self.database_paths})
        self.assertEqual(value['verification']['profile'], self.profiles[-1])
        self.assertFalse(any(command.args[0][0] == '/usr/bin/open' for command in self.run.call_args_list))

    def test_closed_history_exception_keeps_queue_and_state_fd_ownership_required(self):
        self.successor_fixture()
        for missing in self.database_paths[:2]:
            with self.subTest(missing=missing.name):
                self.owner_files = '\n'.join(f'f{i}\nD{hex(p.stat().st_dev)}\ni{p.stat().st_ino}\nn{p}'
                                             for i, p in enumerate(self.database_paths) if p != missing)
                with self.assertRaisesRegex(NativeError, 'safe default native databases: ' + missing.name):
                    desktop_wake._database_files({'pid': 401}, {'codex_home': str(self.root / '.codex')},
                                                  desktop_wake.files.deadline(3), require_history_handle=False)

    def test_closed_history_exception_rejects_unsafe_or_foreign_history_path(self):
        self.successor_fixture()
        history = self.database_paths[-1]
        self.owner_files = '\n'.join(f'f{i}\nD{hex(p.stat().st_dev)}\ni{p.stat().st_ino}\nn{p}'
                                     for i, p in enumerate(self.database_paths[:-1]))
        def check():
            return desktop_wake._database_files({'pid': 401}, {'codex_home': str(self.root / '.codex')},
                                                 desktop_wake.files.deadline(3), require_history_handle=False)
        self.assertIn(str(history), check())
        history.chmod(0o666)
        with self.assertRaisesRegex(NativeError, 'safe default native databases: thread_history'):
            check()
        history.chmod(0o644)
        original_lstat = Path.lstat
        def foreign_owner(path, *args, **kwargs):
            facts = original_lstat(path, *args, **kwargs)
            return (SimpleNamespace(st_uid=facts.st_uid + 1, st_mode=facts.st_mode,
                                    st_dev=facts.st_dev, st_ino=facts.st_ino)
                    if path == history else facts)
        with patch.object(Path, 'lstat', foreign_owner):
            with self.assertRaisesRegex(NativeError, 'safe default native databases: thread_history'):
                check()
        moved = history.with_suffix('.original')
        history.rename(moved)
        history.symlink_to(moved)
        with self.assertRaisesRegex(NativeError, 'safe default native databases: thread_history'):
            check()
        history.unlink()
        history.mkdir()
        with self.assertRaisesRegex(NativeError, 'safe default native databases: thread_history'):
            check()

    def test_optional_history_fd_still_rejects_any_present_stale_mapping(self):
        self.successor_fixture()
        for path in self.database_paths:
            with self.subTest(database=path.name):
                replacement = path.with_suffix('.replacement')
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(0o644)
                os.replace(replacement, path)
                with self.assertRaisesRegex(NativeError, 'FD differs from its pathname: ' + path.name):
                    desktop_wake._database_files({'pid': 401}, {'codex_home': str(self.root / '.codex')},
                                                  desktop_wake.files.deadline(3), require_history_handle=False)
                self.owner_files = '\n'.join(f'f{i}\nD{hex(p.stat().st_dev)}\ni{p.stat().st_ino}\nn{p}'
                                             for i, p in enumerate(self.database_paths))

    def test_successor_ambiguity_unsupported_args_or_missing_database_stay_held(self):
        identity = self.successor_fixture()
        initial = self.inventory
        self.inventory += f'601 {os.getuid()} {self.app}\n'
        with self.assertRaisesRegex(NativeError, 'ambiguous'):
            desktop_wake.discover_successor(identity)
        self.inventory = initial
        old = self.commands[401]
        self.commands[401] = (str(self.owner), (str(self.owner), 'app-server', '-c', 'sqlite_home="/other"'))
        with self.assertRaisesRegex(NativeError, 'startup domain'):
            desktop_wake.discover_successor(identity)
        self.commands[401] = old
        self.owner_files = '\n'.join(f'f{i}\nD{hex(p.stat().st_dev)}\ni{p.stat().st_ino}\nn{p}'
                                     for i, p in enumerate(self.database_paths[:-1]))
        with self.assertRaisesRegex(NativeError, 'default native databases'):
            desktop_wake.discover_successor(identity)

    def test_successor_socket_and_database_replacement_or_unsafe_mode_fail_closed(self):
        identity = self.successor_fixture()
        old = self.app_sockets
        self.app_sockets += '\nn' + str(self.pipe)
        # An unrelated socket outside the source-defined directory is ignored.
        self.assertEqual(desktop_wake.discover_successor(identity).session_id, SESSION)
        self.app_sockets = old
        self.database_paths[0].chmod(0o666)
        with self.assertRaisesRegex(NativeError, 'safe default native databases'):
            desktop_wake.discover_successor(identity)
        self.database_paths[0].chmod(0o644)
        real_verify = desktop_wake.verify
        def replace_database(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            path = self.database_paths[0]
            other = path.with_suffix('.new')
            other.write_bytes(path.read_bytes())
            os.replace(other, path)
            return result
        with patch.object(desktop_wake, 'verify', side_effect=replace_database):
            with self.assertRaisesRegex(NativeError, 'database.*(generation changed|FD differs)'):
                desktop_wake.discover_successor(identity)

    def test_same_binary_cli_is_ignored_but_two_eligible_app_backends_are_ambiguous(self):
        identity = self.successor_fixture()
        self.inventory += f'601 {os.getuid()} {self.owner}\n'
        self.identities[601] = (1, os.getuid(), 'standalone CLI')
        self.commands[601] = (str(self.owner), (str(self.owner),))
        self.assertEqual(desktop_wake.discover_successor(identity).session_id, SESSION)
        self.identities[601] = (501, os.getuid(), 'second app backend')
        self.commands[601] = self.commands[401]
        with self.assertRaisesRegex(NativeError, 'eligible backend.*ambiguous'):
            desktop_wake.discover_successor(identity)

    def test_stale_database_fd_name_does_not_prove_current_path_inode_ownership(self):
        identity = self.successor_fixture()
        path = self.database_paths[0]
        replacement = path.with_suffix('.new')
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        with self.assertRaisesRegex(NativeError, 'FD differs'):
            desktop_wake.discover_successor(identity)

    def test_kernel_peer_check_sends_no_protocol_bytes_and_matches_actual_app_pid(self):
        value = self.identity().evidence['desktop_wake']
        with patch.object(desktop_wake.socket, 'socket') as factory:
            client = factory.return_value.__enter__.return_value
            client.getsockopt.return_value = struct.pack('i', 301)
            REAL_PEER_OWNER(value, desktop_wake.files.deadline(1))
            client.connect.assert_called_once_with(str(self.pipe))
            client.getsockopt.assert_called_once_with(0, 2, struct.calcsize('i'))
            client.send.assert_not_called()
            client.sendall.assert_not_called()
            client.write.assert_not_called()

    def test_kernel_peer_wrong_pid_rebind_or_timeout_cannot_certify_socket_ownership(self):
        value = self.identity().evidence['desktop_wake']
        with patch.object(desktop_wake.socket, 'socket') as factory:
            client = factory.return_value.__enter__.return_value
            client.getsockopt.return_value = struct.pack('i', 999)
            with self.assertRaisesRegex(NativeError, 'another process'):
                REAL_PEER_OWNER(value, desktop_wake.files.deadline(1))
            client.getsockopt.return_value = struct.pack('i', 301)
            changed = {**value['pipe'], 'inode': value['pipe']['inode'] + 1}
            with patch.object(desktop_wake, '_socket', side_effect=[value['pipe'], changed]):
                with self.assertRaisesRegex(NativeError, 'changed during connection'):
                    REAL_PEER_OWNER(value, desktop_wake.files.deadline(1))
            client.connect.side_effect = socket.timeout('bounded connect timed out')
            with self.assertRaises(socket.timeout):
                REAL_PEER_OWNER(value, desktop_wake.files.deadline(1))
            client.sendall.assert_not_called()

    def test_missing_hint_or_other_platform_does_not_claim_desktop(self):
        with patch.object(desktop_wake, "_inspect", side_effect=AssertionError("unexpected process inspection")):
            self.assertIsNone(desktop_wake.capture({"HOME": str(self.root)}))
            with patch.object(desktop_wake.sys, "platform", "linux"):
                self.assertIsNone(desktop_wake.capture(self.env))

    def test_cli_inheriting_desktop_environment_cannot_select_an_outer_app(self):
        self.identities[201] = (401, os.getuid(), "nearer CLI birth")
        worker = str(self.root / "terminal-worker")
        self.identities[401] = (301, os.getuid(), "worker birth")
        self.commands[401] = (worker, (worker,))
        self.assertIsNone(desktop_wake.capture(self.env))

    def test_ambiguous_invoking_process_is_an_error_instead_of_cli_fallback(self):
        with patch.object(desktop_wake.process, "inspect", side_effect=OSError("unavailable")):
            with self.assertRaises(NativeError):
                desktop_wake.capture(self.env)

    def test_capture_rejects_foreign_owner_or_ambiguous_argument_identity(self):
        original_identity, original_command = self.identities[201], self.commands[201]
        self.identities[201] = (301, os.getuid() + 1, "owner birth")
        with self.assertRaises(NativeError):
            desktop_wake.capture(self.env)
        self.identities[201] = original_identity
        self.commands[201] = (str(self.owner), ("/ambiguous/env", "app-server"))
        with self.assertRaises(NativeError):
            desktop_wake.capture(self.env)
        self.commands[201] = original_command
        self.run.assert_not_called()

    def test_custom_native_and_app_profiles_are_visible_but_cannot_pass_preflight(self):
        for name in ("HOME", "CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_ELECTRON_USER_DATA_PATH"):
            with self.subTest(name=name):
                descriptor = desktop_wake.capture({**self.env, name: str(self.root / "other")})
                self.assertFalse(descriptor["domain"]["supported"])
                with self.assertRaises(NativeError):
                    desktop_wake.verify(self.identity(descriptor))
        self.run.assert_not_called()

    def test_unsupported_backend_arguments_and_app_userdata_switch_cannot_wake(self):
        original = dict(self.commands)
        variants = ((201, (str(self.owner), "app-server", "-c", 'sqlite_home="/other"')),
                    (301, (str(self.app), "--user-data-dir=/other")))
        for pid, argv in variants:
            self.commands = dict(original)
            self.commands[pid] = (self.commands[pid][0], argv)
            with self.subTest(pid=pid):
                descriptor = desktop_wake.capture(self.env)
                self.assertFalse(descriptor["startup_supported"])
                with self.assertRaises(NativeError):
                    desktop_wake.verify(self.identity(descriptor))

    def test_verification_returns_separate_proof_without_changing_target_epoch_or_capture(self):
        identity = self.identity()
        before = deepcopy(identity)
        verified = desktop_wake.verify(identity)
        self.assertEqual(identity, before)
        self.assertIsNone(identity.epoch)
        self.assertNotIn("verification", identity.evidence["desktop_wake"])
        self.assertEqual(verified["verification"]["profile"], self.profiles[-1])
        self.assertEqual(set(verified["verification"]["files"]),
                         {str(self.info), str(self.app), str(self.asar), str(self.owner), str(self.launcher)})

    def test_full_queue_configuration_must_match_the_captured_local_domain(self):
        identity = self.identity()
        variants = ({**identity.target, "cli_args": ["--profile", "other"]},
                    {**identity.target, "env": {**identity.target["env"], "HOME": str(self.root / "other")}},
                    {**identity.target, "env": {**identity.target["env"], "CODEX_SQLITE_HOME": "/other"}},
                    {**identity.target, "binary": str(self.root / "other-codex")})
        for target in variants:
            with self.subTest(target=target), self.assertRaises(NativeError):
                desktop_wake.verify(replace(identity, target=target))
        self.run.assert_not_called()

    def test_bundled_queue_launcher_is_pinned_and_frozen_in_verified_proof(self):
        identity = self.identity()
        identity = replace(identity, target={**identity.target, "binary": str(self.launcher)})
        verified = desktop_wake.verify(identity)
        self.assertIn(str(self.launcher), verified["verification"]["files"])
        self.launcher.write_bytes(b"changed producer under the same path")
        self.run.reset_mock()
        self.assertNotEqual(desktop_wake.verify(identity)["verification"]["profile"],
                            verified["verification"]["profile"])
        self.run.reset_mock()
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
        self.run.assert_not_called()

    def test_unseen_build_and_source_bytes_can_be_observed_and_woken(self):
        for path in (self.app, self.asar, self.owner, self.launcher):
            path.write_bytes(path.read_bytes() + b" previously unmeasured update")
        self.info.write_bytes(plistlib.dumps({**self.metadata,
            "CFBundleShortVersionString": "future-version", "CFBundleVersion": "not-in-any-table"}))
        identity = self.identity()
        verified = desktop_wake.verify(identity)
        profile = verified["verification"]["profile"]
        self.assertEqual((profile["version"], profile["build"]), ("future-version", "not-in-any-table"))
        self.assertEqual(profile["owner_sha256"], self.digest(self.owner))
        self.run.reset_mock()
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "accepted")
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0][-1], "codex://threads/wake/" + SESSION)

    def test_other_app_identity_and_malformed_metadata_cannot_wake(self):
        for key, value in (("CFBundleIdentifier", "someone.else.app"),
                           ("CFBundleExecutable", "OtherApp"), ("CFBundleVersion", None),
                           ("CFBundleShortVersionString", ""), ("CFBundleVersion", "bad\nvalue")):
            self.info.write_bytes(plistlib.dumps({**self.metadata, key: value}, skipkeys=False)
                                  if value is not None else plistlib.dumps({k:v for k,v in self.metadata.items() if k!=key}))
            with self.subTest(key=key, value=value), self.assertRaises(NativeError):
                desktop_wake.verify(self.identity())
        self.run.assert_not_called()

    def test_each_known_app_build_freezes_its_own_complete_profile(self):
        identity = self.identity()
        for profile in self.profiles:
            self.info.write_bytes(plistlib.dumps({**self.metadata,
                "CFBundleShortVersionString": profile["version"], "CFBundleVersion": profile["build"]}))
            with self.subTest(version=profile["version"]):
                verified = desktop_wake.verify(identity)
                self.assertEqual(verified["verification"]["profile"], profile)
                self.run.reset_mock()
                self.assertEqual(desktop_wake.wake(SESSION, verified).status, "accepted")
                self.assertEqual(self.run.call_count, 1)
                self.assertEqual(self.run.call_args.args[0],
                                 ["/usr/bin/open", "-g", "-a", str(self.bundle),
                                  "codex://threads/wake/" + SESSION])

    def test_frozen_source_hashes_and_file_set_cannot_be_forged(self):
        verified = desktop_wake.verify(self.identity())
        self.run.reset_mock()
        for key in ("app_sha256", "asar_sha256", "owner_sha256", "launcher_sha256"):
            changed = deepcopy(verified)
            changed["verification"]["profile"][key] = "f" * 64
            with self.subTest(key=key):
                self.assertEqual(desktop_wake.wake(SESSION, changed).status, "uncertain")
        for path in verified["verification"]["files"]:
            changed = deepcopy(verified)
            changed["verification"]["files"].pop(path)
            with self.subTest(missing=path):
                self.assertEqual(desktop_wake.wake(SESSION, changed).status, "uncertain")
        self.run.assert_not_called()

    def test_version_metadata_is_recorded_without_a_build_registry(self):
        for version, build in (("26.1103.32239", "25471"), ("26.1103.60933", "25894"),
                               ("26.1106.51428", "26605"), ("26.1111.21308", "27194"),
                               ("26.1111.71704", "29030")):
            with self.subTest(build=build):
                self.info.write_bytes(plistlib.dumps({**self.metadata,
                    "CFBundleShortVersionString": version, "CFBundleVersion": build}))
                profile = desktop_wake.verify(self.identity())["verification"]["profile"]
                self.assertEqual((profile["version"], profile["build"]), (version, build))

    def test_app_update_does_not_relabel_frozen_prior_build_verification(self):
        prior = self.profiles[-2]
        self.info.write_bytes(plistlib.dumps({**self.metadata,
            "CFBundleShortVersionString": prior["version"], "CFBundleVersion": prior["build"]}))
        verified = desktop_wake.verify(self.identity())
        self.assertEqual(verified["verification"]["profile"], prior)
        self.info.write_bytes(plistlib.dumps(self.metadata))
        self.run.reset_mock()
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
        self.assertEqual(verified["verification"]["profile"], prior)
        self.run.assert_not_called()

    def test_backend_or_app_generation_change_rejects_verification(self):
        identity = self.identity()
        for pid in (201, 301):
            original = self.identities[pid]
            self.identities[pid] = (*original[:2], "replacement birth")
            with self.subTest(pid=pid), self.assertRaises(NativeError):
                desktop_wake.verify(identity)
            self.identities[pid] = original

    def test_changed_backend_arguments_under_same_pid_and_birth_cannot_reuse_capture(self):
        identity = self.identity()
        verified = desktop_wake.verify(identity)
        executable, argv = self.commands[201]
        self.commands[201] = (executable, (*argv, "--analytics-default-enabled"))
        self.run.reset_mock()
        with self.assertRaises(NativeError):
            desktop_wake.verify(identity)
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
        self.run.assert_not_called()

    def test_process_inspection_deadline_cannot_capture_verify_or_schedule_open(self):
        identity = self.identity()
        verified = desktop_wake.verify(identity)
        for phase in ("capture", "verify", "wake"):
            now = [0]
            def inspect(pid):
                if pid == 201:
                    now[0] = 1
                return self.commands[pid]
            self.run.reset_mock()
            with self.subTest(phase=phase), \
                    patch.object(desktop_wake.files.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(desktop_wake.process, "inspect", side_effect=inspect):
                if phase == "wake":
                    self.assertEqual(desktop_wake.wake(SESSION, verified, timeout=.5).status, "uncertain")
                else:
                    with self.assertRaises(NativeError):
                        if phase == "capture":
                            desktop_wake.capture(self.env, timeout=.5)
                        else:
                            desktop_wake.verify(identity, timeout=.5)
                self.run.assert_not_called()

    def test_socket_symlink_writability_replacement_and_wrong_owner_are_rejected(self):
        identity = self.identity()
        self.pipe.chmod(0o666)
        with self.assertRaises(NativeError):
            desktop_wake.capture(self.env)
        self.pipe.chmod(0o600)
        link = self.root / "link.sock"
        link.symlink_to(self.pipe)
        with self.assertRaises(NativeError):
            desktop_wake.capture({**self.env, "CODEX_APP_TOOLS_PIPE_PATH": str(link)})
        old = self.root / "old.sock"
        self.pipe.rename(old)
        replacement = socket.socket(socket.AF_UNIX)
        self.addCleanup(replacement.close)
        replacement.bind(str(self.pipe))
        self.pipe.chmod(0o600)
        with self.assertRaises(NativeError):
            desktop_wake.verify(identity)
        identity = self.identity()
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout="p301\nn/another/socket\n")
        self.run.side_effect = None
        with self.assertRaises(NativeError):
            desktop_wake.verify(identity)

    def test_wake_is_one_background_url_with_no_prompt_and_no_admission_claim(self):
        identity = self.identity()
        verified = desktop_wake.verify(identity)
        before = deepcopy(verified)
        self.run.reset_mock()
        result = desktop_wake.wake(SESSION, verified)
        self.assertEqual(result.status, "accepted", result)
        self.assertIn("admission is unconfirmed", result.detail)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0],
                         ["/usr/bin/open", "-g", "-a", str(self.bundle), "codex://threads/wake/" + SESSION])
        self.assertNotIn("shell", self.run.call_args.kwargs)
        self.assertEqual(verified, before)
        self.assertIsNone(identity.epoch)

    def test_open_failure_timeout_or_nonzero_exit_is_uncertain_without_retry(self):
        verified = desktop_wake.verify(self.identity())
        for failure in (OSError("launch unavailable"), subprocess.TimeoutExpired("open", 1),
                        subprocess.CompletedProcess([], 1)):
            self.run.reset_mock()
            self.run.side_effect = failure if isinstance(failure, BaseException) else None
            self.run.return_value = failure
            with self.subTest(failure=failure):
                self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
                self.assertEqual(self.run.call_count, 1)

    def test_wake_refuses_invalid_uuid_unverified_proof_or_changed_generation_before_open(self):
        verified = desktop_wake.verify(self.identity())
        self.run.reset_mock()
        for value in ("not-a-thread", SESSION + "?prompt=hello", SESSION + "/other"):
            with self.subTest(value=value):
                self.assertEqual(desktop_wake.wake(value, verified).status, "uncertain")
        unverified = deepcopy(verified)
        unverified.pop("verification")
        self.assertEqual(desktop_wake.wake(SESSION, unverified).status, "uncertain")
        self.identities[301] = (1, os.getuid(), "replacement app")
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
        self.run.assert_not_called()


class DesktopHelperWakeTests(DesktopWakeFixture):
    def setUp(self):
        super().setUp()
        self.direct_profiles = self.profiles
        profiles = []
        for qualified in FIXTURE_PROFILES:
            if not qualified.get("helper_relative_path"):
                continue
            helper = self.bundle / qualified["helper_relative_path"]
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_bytes(b"synthetic qualified utility helper")
            profiles.append({**qualified, "app_sha256": self.digest(self.app),
                             "owner_sha256": self.digest(self.owner), "asar_sha256": self.digest(self.asar),
                             "launcher_sha256": self.digest(self.launcher), "helper_sha256": self.digest(helper)})
        self.profiles = tuple(profiles)
        self.select_profile(self.profiles[-1])

    def select_profile(self, profile):
        self.profile = profile
        self.helper = self.bundle / profile["helper_relative_path"]
        self.metadata.update(CFBundleShortVersionString=profile["version"], CFBundleVersion=profile["build"])
        self.info.write_bytes(plistlib.dumps(self.metadata))
        self.identities[201] = (251, os.getuid(), "owner birth")
        self.identities[251] = (301, os.getuid(), "helper birth")
        self.commands[251] = (str(self.helper), (str(self.helper), "--type=utility",
                                               "--utility-sub-type=node.mojom.NodeService",
                                               "--service-sandbox-type=none"))

    def launch_cwd_suffix(self):
        home = str(self.root / ".codex")
        script = str(self.bundle / "Contents/Resources/cua_node/bin/node_repl")
        return ("--plugin-mcp-launch-cwd", "unified-computer-use@openai-bundled", "cua_repl", home,
                "--mcp-launch-cwd", "node_repl", script, home,
                "--mcp-launch-cwd", "artifact_session", script, home)

    def successor_fixture(self):
        identity = super().successor_fixture()
        uid = os.getuid()
        self.identities[401] = (451, uid, "successor owner")
        self.identities[451] = (501, uid, "successor helper")
        self.commands[451] = self.commands[251]
        self.inventory += f"451 {uid} {self.helper}\n"
        return identity

    def test_helper_capture_is_deterministic_and_does_not_hash_connect_or_run_commands(self):
        with patch.object(desktop_wake.files, "executable_sha256", side_effect=AssertionError("capture hashed")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("capture connected")):
            first, second = desktop_wake.capture(self.env), desktop_wake.capture(self.env)
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], 1)
        self.assertEqual((first["owner"]["pid"], first["owner"]["parent_pid"],
                          first["helper"]["pid"], first["helper"]["parent_pid"], first["app"]["pid"]),
                         (201, 251, 251, 301, 301))
        self.assertEqual(set(first["helper"]), {"pid", "parent_pid", "uid", "birth", "executable", "argv_sha256"})
        self.assertEqual(first["helper"]["executable"], str(self.helper))
        self.assertTrue(first["startup_supported"])
        self.assertTrue(first["domain"]["supported"])
        self.run.assert_not_called()

    def test_helper_source_is_observed_and_frozen_in_the_delivery_proof(self):
        self.assertEqual({profile["build"] for profile in self.profiles}, {"29513"})
        self.assertEqual(self.profile["helper_relative_path"],
                         "Contents/Frameworks/Codex Framework.framework/Versions/158.0.8127.37"
                         "/Helpers/Codex (Aperitif).app/Contents/MacOS/Codex (Aperitif)")
        self.assertIs(self.profile["launch_cwd_args"], True)
        identity = self.identity()
        identity = replace(identity, target={**identity.target, "binary": str(self.launcher)})
        before = deepcopy(identity)
        verified = desktop_wake.verify(identity)
        self.assertEqual(identity, before)
        self.assertEqual(verified["verification"]["profile"], self.profile)
        self.assertEqual(set(verified["verification"]["files"]),
                         {str(path) for path in (self.info, self.app, self.asar,
                                                self.owner, self.helper, self.launcher)})
        self.run.reset_mock()
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "accepted")
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0], ["/usr/bin/open", "-g", "-a", str(self.bundle),
                                                    "codex://threads/wake/" + SESSION])

    def test_unseen_app_build_and_framework_version_use_actual_helper_identity(self):
        helper = self.bundle / self.profile["helper_relative_path"].replace("158.0.8127.37", "999.1.2.3")
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"new previously unmeasured helper bytes")
        self.info.write_bytes(plistlib.dumps({**self.metadata,
            "CFBundleShortVersionString": "future-helper-release", "CFBundleVersion": "999999"}))
        _, argv = self.commands[251]
        self.commands[251] = (str(helper), (str(helper), *argv[1:]))
        verified = desktop_wake.verify(self.identity())
        observed = verified["verification"]["profile"]
        self.assertEqual(observed["build"], "999999")
        self.assertEqual(observed["helper_relative_path"], str(helper.relative_to(self.bundle)))
        self.assertEqual(observed["helper_sha256"], self.digest(helper))
        self.run.reset_mock()
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "accepted")
        self.assertEqual(self.run.call_count, 1)

    def test_frozen_helper_hash_and_generation_cannot_be_forged(self):
        verified = desktop_wake.verify(self.identity())
        self.run.reset_mock()
        for change in ("hash", "missing-file", "generation"):
            changed = deepcopy(verified)
            if change == "hash":
                changed["verification"]["profile"]["helper_sha256"] = "f" * 64
            elif change == "missing-file":
                changed["verification"]["files"].pop(str(self.helper))
            else:
                changed["helper"]["birth"] = "not the captured helper generation"
            with self.subTest(change=change):
                self.assertEqual(desktop_wake.wake(SESSION, changed).status, "uncertain")
        self.run.assert_not_called()

    def test_helper_descriptor_requires_an_exact_distinct_app_helper_owner_chain(self):
        captured = desktop_wake.capture(self.env)
        variants = []
        missing = deepcopy(captured)
        missing.pop("helper")
        variants.append(("missing", missing))
        for name, key, value in (("malformed", "helper", None),
                                 ("bundle", "app_bundle", str(self.root / "Other.app"))):
            changed = deepcopy(captured)
            changed[key] = value
            variants.append((name, changed))
        for name, component, key, value in (
                ("parent", "helper", "parent_pid", 999), ("owner parent", "owner", "parent_pid", 301),
                ("wrong helper", "helper", "executable", str(self.root / self.helper.name)),
                ("wrong app", "app", "executable", str(self.root / "Other.app/Contents/MacOS/ChatGPT")),
                ("app/helper duplicate", "helper", "pid", 301),
                ("owner/helper duplicate", "helper", "pid", 201),
                ("app/owner duplicate", "owner", "pid", 301),
                ("malformed pid", "helper", "pid", True), ("malformed parent", "helper", "parent_pid", True),
                ("malformed argv", "helper", "argv_sha256", "bad")):
            changed = deepcopy(captured)
            changed[component][key] = value
            variants.append((name, changed))
        for name, descriptor in variants:
            with self.subTest(name=name), self.assertRaises(NativeError):
                desktop_wake.verify(self.identity(descriptor))
        self.run.assert_not_called()

    def test_app_build_does_not_choose_topology_but_extra_hops_stay_invalid(self):
        identity = self.identity()
        other = next(p for p in self.direct_profiles if p["build"] == "29030")
        self.info.write_bytes(plistlib.dumps({**self.metadata,
            "CFBundleShortVersionString": other["version"], "CFBundleVersion": other["build"]}))
        observed = desktop_wake.verify(identity)
        self.assertEqual(observed["verification"]["profile"]["build"], other["build"])
        self.assertEqual(observed["verification"]["profile"]["helper_relative_path"],
                         self.profile["helper_relative_path"])
        self.info.write_bytes(plistlib.dumps(self.metadata))
        self.identities[251] = (275, os.getuid(), "helper birth")
        worker = str(self.root / "extra-worker")
        self.identities[275] = (301, os.getuid(), "extra hop")
        self.commands[275] = (worker, (worker,))
        self.assertIsNone(desktop_wake.capture(self.env))

    def test_helper_generation_uid_arguments_path_and_file_changes_invalidate_frozen_proof(self):
        identity = self.identity()
        verified = desktop_wake.verify(identity)
        original_identity, original_command = self.identities[251], self.commands[251]
        variants = (("birth", (301, os.getuid(), "replacement helper"), original_command),
                    ("uid", (301, os.getuid() + 1, "helper birth"), original_command),
                    ("argv", original_identity, (str(self.helper), (*original_command[1], "--changed"))),
                    ("path", original_identity, (str(self.root / self.helper.name), original_command[1])))
        for name, facts, command in variants:
            self.identities[251], self.commands[251] = facts, command
            self.run.reset_mock()
            with self.subTest(name=name):
                with self.assertRaises(NativeError):
                    desktop_wake.verify(identity)
                self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
                self.run.assert_not_called()
        self.identities[251], self.commands[251] = original_identity, original_command
        self.helper.write_bytes(b"changed helper under the same pathname")
        self.run.reset_mock()
        current = desktop_wake.verify(identity)
        self.assertEqual(current["verification"]["profile"]["helper_sha256"], self.digest(self.helper))
        self.run.reset_mock()
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
        self.run.assert_not_called()

    def test_helper_file_safety_and_hash_read_race_fail_closed(self):
        identity = self.identity()
        self.helper.chmod(0o666)
        with self.assertRaises(NativeError):
            desktop_wake.verify(identity)
        self.helper.chmod(0o644)
        digest = desktop_wake.files.executable_sha256
        def replace_helper(path, until):
            result = digest(path, until)
            if path == self.helper:
                replacement = self.helper.with_suffix(".replacement")
                replacement.write_bytes(self.helper.read_bytes())
                os.replace(replacement, self.helper)
            return result
        with patch.object(desktop_wake.files, "executable_sha256", side_effect=replace_helper), \
                self.assertRaises(NativeError):
            desktop_wake.verify(identity)
        self.run.assert_not_called()

    def test_helper_process_generation_race_during_capture_is_an_error(self):
        calls = []
        def identity(pid, **options):
            calls.append(pid)
            result = self.identities[pid]
            return (*result[:2], "replacement helper") if pid == 251 and calls.count(pid) > 1 else result
        with patch.object(desktop_wake.process, "identity", side_effect=identity), self.assertRaises(NativeError):
            desktop_wake.capture(self.env)
        self.assertEqual(calls.count(251), 2)
        self.run.assert_not_called()

    def test_same_helper_binary_requires_its_node_service_role(self):
        executable, argv = self.commands[251]
        variants = ((executable,),
                    (executable, "--type=gpu-process", *argv[2:]),
                    (executable, argv[1], "--utility-sub-type=network.mojom.NetworkService", argv[3]),
                    (*argv[:3], "--service-sandbox-type=network"),
                    (*argv, "--type=utility"))
        for command in variants:
            self.commands[251] = (executable, command)
            with self.subTest(argv=command):
                captured = desktop_wake.capture(self.env)
                self.assertFalse(captured["startup_supported"])
                with self.assertRaises(NativeError):
                    desktop_wake.verify(self.identity(captured))
        self.commands[251] = (executable, (*argv, "--lang=en-US"))
        self.assertTrue(desktop_wake.capture(self.env)["startup_supported"])
        self.run.assert_not_called()

    def test_helper_or_app_death_alone_does_not_release_the_recorded_owner(self):
        identity = self.identity()
        with patch.object(desktop_wake.os, "kill", return_value=None):
            self.assertEqual(desktop_wake.owner_process_state(identity)["status"], "alive")
            for pid, component in ((251, "helper"), (301, "app")):
                original = self.identities[pid]
                self.identities[pid] = (*original[:2], "replacement generation")
                with self.subTest(component=component):
                    state = desktop_wake.owner_process_state(identity)
                    self.assertEqual(state["status"], "unknown")
                    self.assertEqual(state["components"][component]["status"], "ended")
                    self.assertEqual(state["components"]["owner"]["status"], "alive")
                self.identities[pid] = original
            self.identities[201] = (251, os.getuid(), "replacement backend")
            self.assertEqual(desktop_wake.owner_process_state(identity)["status"], "ended")

    def test_complete_launch_cwd_suffix_requires_the_supported_helper_topology(self):
        executable, base = self.commands[201]
        suffix = self.launch_cwd_suffix()
        direct = next(p for p in self.direct_profiles if p["build"] == "29030")
        for profile in (direct, self.profile):
            self.metadata.update(CFBundleShortVersionString=profile["version"], CFBundleVersion=profile["build"])
            self.info.write_bytes(plistlib.dumps(self.metadata))
            self.identities[201] = (301 if profile == direct else 251, os.getuid(), "owner birth")
            for tail in ((), suffix):
                self.commands[201] = (executable, base + tail)
                with self.subTest(build=profile["build"], suffix=bool(tail)):
                    captured = desktop_wake.capture(self.env)
                    accepted = not tail or profile["build"] == "29513"
                    self.assertEqual(captured["startup_supported"], accepted)
                    if accepted:
                        self.assertEqual(desktop_wake.verify(self.identity(captured))["verification"]["profile"], profile)
                    else:
                        with self.assertRaises(NativeError):
                            desktop_wake.verify(self.identity(captured))

    def test_partial_reordered_or_modified_launch_cwd_suffixes_stay_unsupported(self):
        executable, base = self.commands[201]
        suffix = self.launch_cwd_suffix()
        variants = [("truncated " + str(length), suffix[:length]) for length in range(1, len(suffix))]
        variants += [("reordered", suffix[4:8] + suffix[:4] + suffix[8:]),
                     ("duplicate", suffix + suffix[:4]), ("trailing option", suffix + ("--remote",))]
        for index in (1, 2, 3, 5, 6, 7, 9, 10, 11):
            changed = list(suffix)
            changed[index] += "-other"
            variants.append(("modified " + str(index), tuple(changed)))
        for name, tail in variants:
            self.commands[201] = (executable, base + tail)
            with self.subTest(name=name):
                captured = desktop_wake.capture(self.env)
                self.assertFalse(captured["startup_supported"])
                with self.assertRaises(NativeError):
                    desktop_wake.verify(self.identity(captured))
        self.run.assert_not_called()

    def test_launch_cwd_suffix_does_not_qualify_direct_or_unknown_topology(self):
        executable, base = self.commands[201]
        self.commands[201] = (executable, base + self.launch_cwd_suffix())
        self.identities[201] = (301, os.getuid(), "owner birth")
        captured = desktop_wake.capture(self.env)
        self.assertIsNotNone(captured)
        self.assertFalse(captured["startup_supported"])
        with self.assertRaises(NativeError):
            desktop_wake.verify(self.identity(captured))
        self.identities[201] = (251, os.getuid(), "owner birth")
        self.commands[251] = (str(self.root / self.helper.name), (str(self.root / self.helper.name),))
        with self.assertRaises(NativeError):
            desktop_wake.capture(self.env)

    def test_helper_startup_keeps_config_userdata_and_custom_domain_restrictions(self):
        original = dict(self.commands)
        for pid, tail in ((201, ("-c", 'sqlite_home="/other"')), (201, ("--remote", "other")),
                          (301, ("--user-data-dir=/other",))):
            self.commands = dict(original)
            executable, argv = self.commands[pid]
            self.commands[pid] = (executable, argv + tail)
            with self.subTest(pid=pid, tail=tail):
                captured = desktop_wake.capture(self.env)
                self.assertFalse(captured["startup_supported"])
                with self.assertRaises(NativeError):
                    desktop_wake.verify(self.identity(captured))
        self.commands = original
        for name in ("HOME", "CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_ELECTRON_USER_DATA_PATH"):
            with self.subTest(env=name):
                captured = desktop_wake.capture({**self.env, name: str(self.root / "other")})
                self.assertFalse(captured["domain"]["supported"])
                with self.assertRaises(NativeError):
                    desktop_wake.verify(self.identity(captured))
        self.run.assert_not_called()

    def test_helper_successor_excludes_remote_backend_and_requires_one_local_chain(self):
        identity = self.successor_fixture()
        self.identities[601] = (501, os.getuid(), "remote backend")
        self.commands[601] = (str(self.owner), (str(self.owner), "app-server", "--remote", "host"))
        self.inventory += f"601 {os.getuid()} {self.owner}\n"
        successor = desktop_wake.discover_successor(identity)
        captured = successor.evidence["desktop_wake"]
        self.assertEqual((captured["owner"]["pid"], captured["helper"]["pid"], captured["app"]["pid"]),
                         (401, 451, 501))
        self.assertEqual(captured["verification"]["profile"], self.profile)
        self.assertEqual(successor.target, identity.target)
        self.assertEqual(set(captured["database_files"]), {str(path) for path in self.database_paths})
        self.identities[601] = (651, os.getuid(), "second local backend")
        self.identities[651] = (501, os.getuid(), "second local helper")
        self.commands[601], self.commands[651] = self.commands[401], self.commands[451]
        self.inventory += f"651 {os.getuid()} {self.helper}\n"
        with self.assertRaisesRegex(NativeError, "eligible backend.*ambiguous"):
            desktop_wake.discover_successor(identity)
        self.assertFalse(any(call.args[0][0] == "/usr/bin/open" for call in self.run.call_args_list))

    def test_helper_successor_still_requires_the_history_database_handle(self):
        identity = self.successor_fixture()
        self.owner_files = "\n".join(f"f{i}\nD{hex(path.stat().st_dev)}\ni{path.stat().st_ino}\nn{path}"
                                     for i, path in enumerate(self.database_paths[:-1]))
        with self.assertRaisesRegex(NativeError, "safe default native databases: thread_history"):
            desktop_wake.discover_successor(identity)


if __name__ == "__main__":
    unittest.main()
