"""Linux Claude profiles remain distinct from Mac and freeze the PID domain."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from murmur.native import claude, lineage, process
from murmur.native.types import IdentityError


class LinuxClaudeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.config = self.home / '.claude'
        (self.config / 'sessions').mkdir(parents=True)
        self.engine, self.child = str(uuid.uuid4()), str(uuid.uuid4())
        self.domain = 'linux:' + 'a' * 32 + ':pid:[4026531836]'
        self.birth = '2250001234'
        self.record = dict(pid=2300, pidDomain=self.domain, procStart=self.birth,
                           sessionId=self.engine, hostSessionId=None, entrypoint='cli',
                           kind='interactive', version='2.1.311', status='idle')
        self.registry = self.config / 'sessions/2300.json'
        self.write_record()
        self.executable = self.home / '.local/share/claude/versions/2.1.311'
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b'fixture Linux Claude executable')
        self.argv = (str(self.executable),)
        self.env = {'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.config),
                    'CLAUDE_PID': '2300', 'CLAUDE_CODE_SESSION_ID': self.engine}
        self.processes = {2400: (2300, os.getuid(), '2250001235'),
                          2300: (1, os.getuid(), self.birth)}
        for active in (
            patch.object(claude, '_platform', return_value='linux'),
            patch.object(process, 'pid_domain', side_effect=lambda: self.domain),
            patch.object(os, 'getppid', return_value=2400),
            patch.object(process, 'identity', side_effect=lambda pid, timeout=3: self.processes[pid]),
            patch.object(process, 'inspect', side_effect=lambda pid: (str(self.executable), self.argv)),
            patch.object(claude, 'LINUX_CLI_SHA256', hashlib.sha256(self.executable.read_bytes()).hexdigest()),
        ):
            active.start()
            self.addCleanup(active.stop)

    def write_record(self, **changes):
        self.record.update(changes)
        self.registry.write_text(json.dumps(self.record))

    def capture(self, event='SessionStart', session=None, **fields):
        engine = session or self.engine
        return claude.capture_creation({'hook_event_name': event, 'session_id': engine, **fields},
                                       {**self.env, 'CLAUDE_CODE_SESSION_ID': engine})

    def root(self):
        observation = self.capture(source='startup')
        identity = claude.current_identity(env=self.env)
        return claude.resolve_identity(identity, lineage_proof={'observations': [observation]})

    def test_pinned_linux_startup_retains_exact_domain_and_platform_in_certificates(self):
        identity = self.root()
        self.assertIsNone(identity.parent_session_id)
        self.assertTrue(identity.parent_known)
        self.assertEqual(identity.target['version'], '2.1.311')
        self.assertEqual(identity.target['platform'], 'linux')
        self.assertEqual(identity.target['pid_domain'], self.domain)
        self.assertEqual(json.loads(identity.epoch), [self.engine, 2300, self.birth, self.domain])
        proof = identity.evidence['parent_proof']
        self.assertEqual((proof['platform'], proof['pid_domain']), ('linux', self.domain))
        self.assertEqual(claude.verify_target(identity), identity)

    def test_hooks_only_profile_accepts_post_tool_without_allowing_other_overrides(self):
        settings = self.home / 'hooks.json'
        settings.write_text(json.dumps({'hooks': {'PostToolUse': []}}))
        settings.chmod(0o600)
        argv = (str(self.executable), '--setting-sources=', '--settings=' + str(settings))
        self.assertTrue(claude._plain_arguments(argv, self.executable))
        for document in ({'hooks': {'UnknownEvent': []}},
                         {'hooks': {'PostToolUse': []}, 'permissions': {'defaultMode': 'bypassPermissions'}}):
            settings.write_text(json.dumps(document))
            self.assertFalse(claude._plain_arguments(argv, self.executable))

    def test_wrong_domain_profile_host_and_birth_are_rejected(self):
        original = self.record.copy()
        for changes in ({'pidDomain': 'linux'}, {'pidDomain': 'darwin'},
                        {'pidDomain': self.domain.replace('a', 'b')}, {'version': '2.1.310'},
                        {'procStart': 'old process birth'},
                        {'hostSessionId': 'local_' + self.child, 'entrypoint': 'claude-desktop'}):
            self.record = {**original, **changes}
            self.write_record()
            with self.subTest(changes=changes), self.assertRaises(IdentityError):
                claude.current_identity(env=self.env)
        self.record = original
        self.write_record()
        with patch.object(claude, 'LINUX_CLI_SHA256', claude.CLI_SHA256), self.assertRaises(IdentityError):
            claude.current_identity(env=self.env)

    def test_linux_exec_or_argv_change_after_hashing_fails_closed(self):
        with patch.object(process, 'inspect', side_effect=[
                (str(self.executable), self.argv), (str(self.executable), ('claude', '--teleport'))]):
            with self.assertRaisesRegex(IdentityError, 'arguments changed'):
                claude.current_identity(env=self.env)

    def test_frozen_target_cannot_move_to_another_namespace_or_lose_its_domain(self):
        identity = self.root()
        for target in ({**identity.target, 'pid_domain': 'darwin'},
                       {k: v for k, v in identity.target.items() if k != 'pid_domain'}):
            with self.subTest(target=target), self.assertRaises(IdentityError):
                claude.verify_target(replace(identity, target=target))
        self.domain = 'linux:' + 'b' * 32 + ':pid:[99]'
        self.write_record(pidDomain=self.domain)
        with self.assertRaises(IdentityError):
            claude.verify_target(identity)
        self.assertEqual(claude.owner_process_state(identity)['status'], 'unknown')

    def test_certified_linux_root_survives_exact_cold_resume(self):
        root = self.root()
        self.birth = '2250999999'
        self.processes[2300] = (1, os.getuid(), self.birth)
        self.write_record(procStart=self.birth)
        self.argv = ('claude', '--resume', self.engine)
        resumed = claude.resolve_identity(claude.current_identity(env=self.env),
                                          lineage_proof=root.evidence['parent_proof'])
        self.assertIsNone(resumed.parent_session_id)
        self.assertNotEqual(resumed.epoch, root.epoch)
        self.assertFalse(resumed.target['root_start_verified'])

    def test_linux_branch_uses_typed_transition_and_preserves_domain(self):
        root = self.root()
        prompt = str(uuid.uuid4())
        end = self.capture('SessionEnd', reason='resume', prompt_id=prompt)
        path = self.config / 'projects/fixture' / f'{self.child}.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'type': 'user', 'sessionId': self.child,
                                   'forkedFrom': {'sessionId': self.engine}}) + '\n')
        start = self.capture(session=self.child, source='fork', prompt_id=prompt, transcript_path=str(path))
        self.write_record(sessionId=self.child)
        child = claude.resolve_identity(claude.identity_from_creation(start), lineage_proof={
            'known': root.evidence['parent_proof']['ancestors'], 'observations': [end, start]})
        self.assertEqual(child.parent_session_id, self.engine)
        self.assertTrue(all(p['pid_domain'] == self.domain for p in child.evidence['parent_proof']['ancestors']))

    def test_platformless_linux_and_cross_platform_known_lineage_are_not_accepted(self):
        facts = self.capture(source='startup')['process']
        for changes in ({'platform': 'darwin'}, {'pid_domain': 'linux'}, {'platform': None}):
            with self.subTest(changes=changes), self.assertRaises(IdentityError):
                lineage.process_key({**facts, **changes})
        mac = {**facts, 'version': '2.1.310'}
        for field in ('platform', 'pid_domain'):
            mac.pop(field)
        observation = lineage.observation({'hook_event_name': 'SessionStart', 'session_id': self.engine,
                                           'source': 'startup'}, mac)
        known = lineage.certify(self.engine, observations=[observation], config_dir=self.config, process=mac)
        with self.assertRaisesRegex(IdentityError, 'another native platform'):
            claude.resolve_identity(claude.current_identity(env=self.env), lineage_proof=known)


if __name__ == '__main__':
    unittest.main()
