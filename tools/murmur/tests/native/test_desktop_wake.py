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


class DesktopWakeTests(unittest.TestCase):
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
                              for profile in desktop_wake._PROFILES)
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
            patch.object(desktop_wake, "_PROFILES", self.profiles),
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
                         {str(self.info), str(self.app), str(self.asar), str(self.owner)})

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
        with self.assertRaises(NativeError):
            desktop_wake.verify(identity)
        self.assertEqual(desktop_wake.wake(SESSION, verified).status, "uncertain")
        self.run.assert_not_called()

    def test_unknown_app_source_backend_or_bundle_version_cannot_pass_preflight(self):
        identity = self.identity()
        for path in (self.app, self.asar, self.owner):
            original = path.read_bytes()
            path.write_bytes(original + b" changed")
            with self.subTest(path=path), self.assertRaises(NativeError):
                desktop_wake.verify(identity)
            path.write_bytes(original)
        self.info.write_bytes(plistlib.dumps({**self.metadata, "CFBundleVersion": "unknown"}))
        with self.assertRaises(NativeError):
            desktop_wake.verify(identity)
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

    def test_new_app_metadata_cannot_fall_back_to_old_profile_hashes(self):
        identity = self.identity()
        identity = replace(identity, target={**identity.target, "binary": str(self.launcher)})
        for key in ("app_sha256", "asar_sha256", "owner_sha256", "launcher_sha256"):
            profiles = (*self.profiles[:-1], {**self.profiles[-1], key: "f" * 64})
            with self.subTest(key=key), patch.object(desktop_wake, "_PROFILES", profiles), \
                    self.assertRaises(NativeError):
                desktop_wake.verify(identity)
            self.run.assert_not_called()

    def test_recent_builds_are_explicitly_supported(self):
        for version, build in (("26.1103.32239", "25471"), ("26.1103.60933", "25894"),
                               ("26.1106.51428", "26605"), ("26.1111.21308", "27194")):
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


if __name__ == "__main__":
    unittest.main()
