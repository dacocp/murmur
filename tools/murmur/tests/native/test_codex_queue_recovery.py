"""Fresh idle recipient preparation uses read-only synthetic native stores."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shlex
import tempfile
import unittest
from unittest.mock import patch

from murmur.native import codex_queue_recovery as recovery
from murmur.native.types import DeliveryResult, NativeError, NativeIdentity


SESSION = '11111111-1111-4111-8111-111111111111'
PARENT = '22222222-2222-4222-8222-222222222222'
SHA_ALPHA5 = '58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea'
SHA_ALPHA6 = '508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44'
SHA_0167 = 'a636331ed87605efc4cf84028ac2884a75363f9d10ead63cffb57ea0016d486e'
SHA_0167_ALPHA4 = '7cb8700a47be3c81bb3c5efbf2d2adc716ce3db4806e9dc992f2925f78b2b194'


class _StoreFixture(unittest.TestCase):
    owner_sha = SHA_ALPHA5

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / '.codex'
        self.rollout = self.home / 'sessions/fixture.jsonl'
        self.rollout.parent.mkdir(parents=True)
        self.header = {'type': 'session_meta', 'ordinal': 0, 'payload': {
            'id': SESSION, 'session_id': SESSION, 'cli_version': '0.164.0-alpha.11.1',
            'history_mode': 'paginated'}}
        self.write_header()
        self.database = self.home / 'state_5.sqlite'
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,source TEXT,archived INTEGER,rollout_path TEXT,history_mode TEXT,cwd TEXT)')
            connection.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                               (SESSION, 'vscode', 0, str(self.rollout), 'paginated', str(self.root)))
        self.database.chmod(0o644)
        self.descriptor = {'schema': 1, 'kind': 'codex-desktop-deep-link-v1',
                           'app_bundle': str(self.root / 'ChatGPT.app'),
                           'app': {'pid': 101, 'birth': 'old app'},
                           'pipe': {'path': str(self.root / 'app.sock'), 'device': 1, 'inode': 2},
                           'startup_supported': True, 'owner': {'pid': 201, 'birth': 'old backend'},
                           'domain': {'codex_home': str(self.home)},
                           'verification': {'profile': {'owner_sha256': self.owner_sha}, 'files': {}}}
        self.identity = NativeIdentity('codex', SESSION, parent_session_id=PARENT, parent_known=True,
            target={'binary': str(self.root / 'codex'), 'cwd': str(self.root), 'cli_args': [],
                    'transport': 'queue', 'env': {'HOME': str(self.root), 'CODEX_HOME': None, 'CODEX_SQLITE_HOME': None}},
            evidence={'desktop_wake': deepcopy(self.descriptor), 'codex_thread_source': 'vscode',
                      'codex_thread_source_session_id': SESSION, 'codex_thread_source_origin': 'app-server:thread/read'})
        self.verify = self.mock(recovery.desktop_wake, 'verify', return_value=deepcopy(self.descriptor))
        self.owner = self.mock(recovery.desktop_wake, 'owner_process_state', return_value={'status': 'ended'})
        self.discover = self.mock(recovery.desktop_wake, 'discover_successor')
        self.mock(recovery.desktop_wake, '_database_files', return_value={'verified-default-dbs': [1, 2]})
        self.mock(recovery.desktop_wake, '_recheck')
        self.current = self.mock(recovery.codex, 'current_identity', side_effect=AssertionError('caller impersonation'))

    def mock(self, obj, name, **options):
        target = patch.object(obj, name, **options)
        self.addCleanup(target.stop)
        return target.start()

    def write_header(self):
        self.rollout.write_text(json.dumps(self.header) + '\n')
        self.rollout.chmod(0o644)

    def row(self, **changes):
        with sqlite3.connect(self.database) as connection:
            for key, value in changes.items():
                connection.execute(f'UPDATE threads SET {key}=? WHERE id=?', (value, SESSION))


class IdlePreparationTests(_StoreFixture):

    def test_healthy_owner_readonly_metadata_preserves_session_parent_target_and_provenance(self):
        original = deepcopy(self.identity)
        before = self.database.read_bytes(), self.rollout.read_bytes()
        result = recovery.prepare_idle_target(self.identity)
        self.assertEqual(self.identity, original)
        self.assertEqual((result.provider, result.session_id, result.parent_session_id, result.parent_known, result.target),
                         (original.provider, original.session_id, original.parent_session_id, original.parent_known, original.target))
        self.assertEqual(result.evidence['codex_thread_source_origin'], 'app-server:thread/read')
        self.assertEqual(result.evidence['codex_idle_thread']['origin'], 'native-state-db')
        self.assertEqual((self.database.read_bytes(), self.rollout.read_bytes()), before)
        self.owner.assert_not_called()
        self.discover.assert_not_called()
        self.current.assert_not_called()

    def test_only_positive_backend_end_allows_actual_successor_evidence_refresh(self):
        successor = replace(self.identity, evidence={**self.identity.evidence, 'desktop_wake': {
            **self.descriptor, 'owner': {'pid': 401, 'birth': 'new backend'}}})
        self.discover.return_value = successor
        self.verify.side_effect = NativeError('old owner ended')
        result = recovery.prepare_idle_target(self.identity)
        self.assertEqual(result.evidence['desktop_wake']['owner']['pid'], 401)
        self.assertEqual(result.target, self.identity.target)
        self.assertEqual(result.parent_session_id, PARENT)
        self.owner.return_value = {'status': 'unknown'}
        self.discover.reset_mock()
        with self.assertRaisesRegex(NativeError, 'not verifiably ended'):
            recovery.prepare_idle_target(self.identity)
        self.discover.assert_not_called()

    def test_unsupported_subagent_and_mismatched_native_source_cannot_be_refreshed(self):
        subagent = {'subAgent': {'thread_spawn': {'parent_thread_id': PARENT, 'depth': 1}}}
        actor = replace(self.identity, evidence={**self.identity.evidence, 'codex_thread_source': subagent})
        with self.assertRaisesRegex(NativeError, 'Spawned Codex subagents'):
            recovery.prepare_idle_target(actor)
        self.verify.assert_not_called()
        self.row(source=json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': PARENT, 'depth': 1}}}))
        with self.assertRaisesRegex(NativeError, 'normal persisted'):
            recovery.prepare_idle_target(self.identity)
        self.discover.assert_not_called()

    def test_archived_missing_ephemeral_or_unsafe_history_is_not_a_fresh_delivery_target(self):
        for changes in ({'archived': 1}, {'rollout_path': None}, {'history_mode': 'ephemeral'},
                        {'rollout_path': str(self.root / 'outside.jsonl')}):
            with self.subTest(changes=changes):
                self.row(archived=0, rollout_path=str(self.rollout), history_mode='paginated')
                self.row(**changes)
                with self.assertRaises(NativeError):
                    recovery.prepare_idle_target(self.identity)
        self.row(archived=0, rollout_path=str(self.rollout), history_mode='paginated')
        self.rollout.chmod(0o666)
        with self.assertRaisesRegex(NativeError, 'safe owned'):
            recovery.prepare_idle_target(self.identity)

    def test_creation_header_matches_exact_thread_without_requiring_current_version(self):
        result = recovery.prepare_idle_target(self.identity)
        self.assertEqual(result.session_id, SESSION)
        for field, value in (('id', PARENT), ('session_id', PARENT), ('history_mode', 'unknown-mode')):
            old = self.header['payload'][field]
            self.header['payload'][field] = value
            self.write_header()
            with self.subTest(field=field):
                with self.assertRaisesRegex(NativeError, 'certified recipient/profile'):
                    recovery.prepare_idle_target(self.identity)
            self.header['payload'][field] = old

    def test_header_creator_does_not_substitute_for_the_verified_append_owner(self):
        version = {SHA_ALPHA5: '0.166.0-alpha.5.1', SHA_ALPHA6: '0.166.0-alpha.6',
                   SHA_0167: '0.167.0-alpha.3.2', SHA_0167_ALPHA4: '0.167.0-alpha.4.1'}[self.owner_sha]
        self.header['payload']['cli_version'] = version
        self.write_header()
        self.assertEqual(recovery.prepare_idle_target(self.identity).session_id, SESSION)
        self.header['payload']['cli_version'] = (
            '0.166.0-alpha.5.1' if self.owner_sha in (SHA_ALPHA6, SHA_0167, SHA_0167_ALPHA4) else '0.166.0-alpha.6')
        self.write_header()
        if self.owner_sha in (SHA_ALPHA6, SHA_0167, SHA_0167_ALPHA4):
            before = self.rollout.read_bytes()
            result = recovery.prepare_idle_target(self.identity)
            self.assertEqual(result.session_id, SESSION)
            self.assertEqual(result.evidence['desktop_wake']['verification']['profile']['owner_sha256'], self.owner_sha)
            self.assertEqual(self.rollout.read_bytes(), before)
        else:
            with self.assertRaisesRegex(NativeError, 'certified recipient/profile'):
                recovery.prepare_idle_target(self.identity)

    def test_state_proof_never_forges_rpc_source_for_legacy_identity(self):
        actor = replace(self.identity, evidence={'desktop_wake': deepcopy(self.descriptor)})
        result = recovery.prepare_idle_target(actor)
        self.assertNotIn('codex_thread_source_origin', result.evidence)
        self.assertEqual(result.evidence['codex_idle_thread']['session_id'], SESSION)

    def test_known_ordinary_creation_sources_can_resume_in_desktop(self):
        for api_source, database_source in recovery._ORDINARY_SOURCE.items():
            with self.subTest(source=api_source):
                self.row(source=database_source)
                actor = replace(self.identity, evidence={**self.identity.evidence, 'codex_thread_source': api_source})
                result = recovery.prepare_idle_target(actor)
                self.assertEqual(result.evidence['codex_idle_thread']['source'], database_source)
                self.assertEqual(result.evidence['codex_thread_source'], api_source)

    def test_cli_and_healthy_older_desktop_keep_their_existing_verified_route(self):
        actor = replace(self.identity, evidence={})
        with patch.object(recovery.codex, 'verify_target', return_value=actor) as verify:
            self.assertEqual(recovery.prepare_idle_target(actor), actor)
            verify.assert_called_once()
        legacy = deepcopy(self.descriptor)
        legacy['verification']['profile']['owner_sha256'] = 'older-qualified-backend'
        self.verify.return_value = legacy
        self.assertNotIn('codex_idle_thread', recovery.prepare_idle_target(self.identity).evidence)

    def test_native_thread_cwd_must_match_captured_hook_configuration_route(self):
        other = self.root / 'other-workspace'
        other.mkdir()
        self.row(cwd=str(other))
        with self.assertRaisesRegex(NativeError, 'cwd differs'):
            recovery.prepare_idle_target(self.identity)

    def test_unqualified_successor_cannot_refresh_or_create_persisted_thread_proof(self):
        descriptor = deepcopy(self.descriptor)
        descriptor['owner'] = {'pid': 401, 'birth': 'new backend'}
        descriptor['verification']['profile']['owner_sha256'] = 'f' * 64
        successor = replace(self.identity, evidence={**self.identity.evidence, 'desktop_wake': descriptor})
        self.verify.side_effect = NativeError('old owner ended')
        self.discover.return_value = successor
        original = deepcopy(self.identity)
        with self.assertRaisesRegex(NativeError, 'not a qualified idle profile'):
            recovery.prepare_idle_target(self.identity)
        with self.assertRaisesRegex(NativeError, 'qualified Desktop backend profile'):
            recovery._persisted_thread(successor, descriptor, recovery.files.deadline(3))
        self.assertEqual(self.identity, original)
        self.current.assert_not_called()


class RetainedQueueTests(_StoreFixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / 'murmur-state'
        self.state.mkdir(mode=0o700)
        self.token = 'A' * 32
        self.message = "You have new messages in 'Fixture'. [wake:" + self.token + ']'
        self.queue_id = '019a0000-0000-7000-8000-000000000001'
        self.queue = self.home / 'queue_1.sqlite'
        self.history = self.home / 'thread_history_1.sqlite'
        with sqlite3.connect(self.queue) as c:
            c.execute('CREATE TABLE queued_items(id TEXT,thread_id TEXT,payload_json TEXT,queue_order INTEGER)')
        with sqlite3.connect(self.history) as c:
            c.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)')
            c.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)', (SESSION, 10, 1))
            c.execute('CREATE TABLE thread_items(thread_id TEXT,item_type TEXT,rollout_ordinal INTEGER,item_json TEXT)')
        self.gate = {'ledger': {'enrollment_id': 'enrollment', 'enrollment_seq': 1, 'user_id': 'account'},
                     'hook': {'key': 'owned-submit', 'currentHash': 'sha256:' + 'a' * 64},
                     'native_hook_failure_policy': 'fail_open'}
        self.gate_mock = self.mock(recovery, '_gate', side_effect=lambda *a, **k: deepcopy(self.gate))
        self.emission = deepcopy(recovery.prepare_idle_handoff(
            self.identity, self.message, self.token, state_root=self.state))
        self.assertIsNotNone(self.emission)
        self.attempt = {'actor': self.identity.actor, 'token': self.token, 'mode': 'idle', 'status': 'pending',
                        'native_identity': asdict(self.identity), 'emission': self.emission, 'evidence': {}}
        descriptor = deepcopy(self.descriptor)
        descriptor['owner'] = {'pid': 401, 'birth': 'new backend'}
        descriptor['app'] = {'pid': 301, 'birth': 'new app'}
        self.successor = replace(self.identity, evidence={**self.identity.evidence, 'desktop_wake': descriptor})
        self.discover.return_value = self.successor
        self.wake = self.mock(recovery.desktop_wake, 'wake', return_value=DeliveryResult('accepted', 'URL only'))

    def enqueue(self, *, queue_id=None, text=None):
        payload = {'UserInput': {'content': [{'type': 'text', 'text': self.message if text is None else text,
                                             'text_elements': []}], 'client_id': 'native-client-correlation'}}
        raw = json.dumps(payload, separators=(',', ':'))
        with sqlite3.connect(self.queue) as c:
            c.execute('INSERT INTO queued_items VALUES(?,?,?,?)', (queue_id or self.queue_id, SESSION, raw, 0))
        return raw

    def probe(self):
        return recovery.probe_idle_recovery(self.identity, self.attempt, state_root=self.state)

    def test_exact_retained_row_can_only_wake_same_id_after_fresh_probe_and_callback(self):
        raw = self.enqueue()
        probe = self.probe()
        self.assertEqual(probe['status'], 'ready', probe)
        self.assertEqual(probe['queue_id'], self.queue_id)
        self.assertEqual(probe['queue_payload_sha256'], hashlib.sha256(raw.encode()).hexdigest())
        events = []
        self.gate_mock.side_effect = lambda *a, **k: events.append('probe') or deepcopy(self.gate)
        self.wake.side_effect = lambda *a, **k: events.append('URL') or DeliveryResult('accepted', 'URL only')
        before = self.queue.read_bytes()
        result = recovery.wake_idle_recovery(SESSION, probe, before_wake=lambda: events.append('callback') or True)
        self.assertEqual(result.status, 'accepted')
        self.assertEqual(events, ['probe', 'callback', 'URL'])
        self.assertEqual(self.queue.read_bytes(), before)
        self.current.assert_not_called()

    def test_absent_duplicate_wrong_body_or_history_contradiction_never_wakes(self):
        self.assertEqual(self.probe()['status'], 'held')
        self.enqueue()
        self.enqueue(queue_id='019a0000-0000-7000-8000-000000000002')
        self.assertIn('duplicate', self.probe()['reason'])
        with sqlite3.connect(self.queue) as c:
            c.execute('DELETE FROM queued_items')
        self.enqueue(text='different body [wake:' + self.token + ']')
        self.assertIn('contradictory', self.probe()['reason'])
        with sqlite3.connect(self.queue) as c:
            c.execute('DELETE FROM queued_items')
        self.enqueue()
        with sqlite3.connect(self.history) as c:
            c.execute('INSERT INTO thread_items VALUES(?,?,?,?)', (SESSION, 'userMessage', 1,
                       json.dumps({'type': 'userMessage', 'content': [{'text': self.message}]})))
        self.assertIn('history already contains', self.probe()['reason'])
        self.wake.assert_not_called()

    def test_original_seal_gate_enrollment_source_and_database_identity_cannot_be_rebound(self):
        self.enqueue()
        self.gate['ledger']['user_id'] = 'new-account'
        self.assertIn('Submit mitigation/enrollment/ledger changed', self.probe()['reason'])
        self.gate['ledger']['user_id'] = 'account'
        self.attempt['evidence']['recovery'] = {'queue_id': 'another-entry'}
        self.assertIn('earlier recovery', self.probe()['reason'])
        self.attempt['evidence'] = {}
        self.owner.return_value = {'status': 'unknown'}
        self.assertIn('not positively ended', self.probe()['reason'])
        self.owner.return_value = {'status': 'ended'}
        replacement = self.queue.with_suffix('.new')
        with sqlite3.connect(replacement) as c:
            c.execute('CREATE TABLE queued_items(id TEXT,thread_id TEXT,payload_json TEXT,queue_order INTEGER)')
        os.replace(replacement, self.queue)
        self.assertEqual(self.probe()['status'], 'held')

    def test_final_probe_row_change_or_callback_rejection_preserves_original_pending_attempt(self):
        self.enqueue()
        probe = self.probe()
        original = deepcopy(self.attempt)
        result = recovery.wake_idle_recovery(SESSION, probe, before_wake=lambda: False)
        self.assertEqual(result.status, 'rejected')
        self.wake.assert_not_called()
        self.gate['hook']['currentHash'] = 'sha256:' + 'b' * 64
        result = recovery.wake_idle_recovery(SESSION, probe, before_wake=lambda: self.fail('gate changed'))
        self.assertEqual(result.status, 'uncertain')
        self.gate['hook']['currentHash'] = 'sha256:' + 'a' * 64
        with sqlite3.connect(self.queue) as c:
            c.execute('DELETE FROM queued_items')
        self.enqueue(queue_id='019a0000-0000-7000-8000-000000000002')
        result = recovery.wake_idle_recovery(SESSION, probe, before_wake=lambda: self.fail('callback precedes fresh proof'))
        self.assertEqual(result.status, 'uncertain')
        self.assertEqual(self.attempt, original)
        self.wake.assert_not_called()

    def test_preexisting_matching_input_stops_new_enqueue_preparation(self):
        self.enqueue()
        with self.assertRaisesRegex(NativeError, 'already exists'):
            recovery.prepare_idle_handoff(self.identity, self.message, self.token, state_root=self.state)

    def test_missing_original_proof_is_not_retroactively_qualified(self):
        self.enqueue()
        self.attempt['emission'] = None
        self.assertEqual(self.probe()['status'], 'held')
        self.discover.assert_not_called()
        self.wake.assert_not_called()

    def test_another_qualified_engine_cannot_recover_the_frozen_handoff(self):
        self.enqueue()
        original = deepcopy(self.attempt)
        queue = self.queue.read_bytes()
        changed = deepcopy(self.successor.evidence['desktop_wake'])
        changed['verification']['profile']['owner_sha256'] = (
            SHA_ALPHA5 if self.owner_sha == SHA_ALPHA6 else SHA_ALPHA6)
        self.discover.return_value = replace(self.successor,
            evidence={**self.successor.evidence, 'desktop_wake': changed})
        result = self.probe()
        self.assertEqual(result['status'], 'held', result)
        self.assertIn('source/profile/domain differs', result['reason'])
        self.assertEqual((self.attempt, self.queue.read_bytes()), (original, queue))
        self.wake.assert_not_called()

    def test_equivalent_install_is_freshly_rechecked_and_recorded_only_after_wake(self):
        self.enqueue()
        equivalent = {'kind': 'codex-submit-release-equivalence-v1',
            'original_release_sha256': 'a' * 64, 'current_release_sha256': 'b' * 64,
            'protected_files_sha256': 'c' * 64}
        with patch.object(recovery, '_gate_equivalence', return_value=deepcopy(equivalent)) as check:
            original = deepcopy(self.attempt)
            probe = self.probe()
            self.assertEqual(probe['gate_equivalence'], equivalent)
            result = recovery.wake_idle_recovery(SESSION, probe, before_wake=lambda: True)
            self.assertEqual(result.status, 'accepted')
            self.assertIn('equivalent Submit install v1', result.detail)
            self.assertIn(equivalent['protected_files_sha256'], result.detail)
            self.assertLess(len(result.detail), 1000)
            self.assertEqual(self.attempt, original)
            self.wake.assert_called_once()
            self.wake.reset_mock()
            check.return_value = {**equivalent, 'current_release_sha256': 'd' * 64}
            result = recovery.wake_idle_recovery(SESSION, probe,
                before_wake=lambda: self.fail('Changed equivalence reached final callback'))
            self.assertEqual(result.status, 'uncertain')
            self.wake.assert_not_called()
            self.assertEqual(self.attempt, original)


class Alpha6IdlePreparationTests(IdlePreparationTests):
    owner_sha = SHA_ALPHA6

    def test_reported_historical_header_prepares_exact_idle_recipient_without_rewriting_native_state(self):
        for creator in ('0.162.0-alpha.2', '0.164.0', '0.166.0-alpha.6', 'unknown historical creator'):
            self.header['payload']['cli_version'] = creator
            self.header['ordinal'] = 8921
            self.write_header()
            original = deepcopy(self.identity)
            before = self.database.read_bytes(), self.rollout.read_bytes()
            with self.subTest(creator=creator):
                result = recovery.prepare_idle_target(self.identity)
                self.assertEqual((result.session_id, result.parent_session_id, result.target),
                                 (original.session_id, original.parent_session_id, original.target))
                self.assertEqual(result.evidence['codex_idle_thread']['source'], 'vscode')
                self.assertEqual(result.evidence['desktop_wake']['verification']['profile']['owner_sha256'], self.owner_sha)
                self.assertEqual((self.database.read_bytes(), self.rollout.read_bytes()), before)
                self.current.assert_not_called()
                self.discover.assert_not_called()

    def test_malformed_creator_and_noninteger_ordinal_cannot_prepare_idle_delivery(self):
        baseline = deepcopy(self.header)
        for creator in (None, '', ' ', ' old', 'old ', 'old\ncreator', 'old\x85creator',
                        'old\ud800creator', 'x' * 129, True, 162, [], {}):
            self.header = deepcopy(baseline)
            self.header['payload']['cli_version'] = creator
            self.write_header()
            with self.subTest(creator=repr(creator)), self.assertRaises(NativeError):
                recovery.prepare_idle_target(self.identity)
        self.header = deepcopy(baseline)
        self.header['payload'].pop('cli_version')
        self.write_header()
        with self.assertRaises(NativeError):
            recovery.prepare_idle_target(self.identity)
        for ordinal in (None, True, -1, '8921', 8921.0):
            self.header = deepcopy(baseline)
            self.header['payload']['cli_version'] = '0.162.0-alpha.2'
            self.header['ordinal'] = ordinal
            self.write_header()
            with self.subTest(ordinal=ordinal), self.assertRaises(NativeError):
                recovery.prepare_idle_target(self.identity)


class Alpha6RetainedQueueTests(RetainedQueueTests):
    owner_sha = SHA_ALPHA6


class Codex0167IdlePreparationTests(Alpha6IdlePreparationTests):
    owner_sha = SHA_0167


class Codex0167RetainedQueueTests(RetainedQueueTests):
    owner_sha = SHA_0167


class Codex0167Alpha4IdlePreparationTests(Codex0167IdlePreparationTests):
    owner_sha = SHA_0167_ALPHA4


class Codex0167Alpha4RetainedQueueTests(Codex0167RetainedQueueTests):
    owner_sha = SHA_0167_ALPHA4


class GateTests(_StoreFixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / 'murmur-state'
        self.state.mkdir(mode=0o700)
        self.ledger = self.state / 'operations.sqlite3'
        with sqlite3.connect(self.ledger) as c:
            c.execute('CREATE TABLE actors(actor TEXT,identity_json TEXT,mode TEXT,enrollment_id TEXT,enrollment_seq INTEGER,user_id TEXT)')
            c.execute('INSERT INTO actors VALUES(?,?,?,?,?,?)',
                       (self.identity.actor, json.dumps(asdict(self.identity)), 'native', 'enrolled', 1, 'account'))
        self.release = self.root / 'release'
        source = self.release / 'tools/murmur'
        self.runtime_files = {}
        for name in ('__main__.py', 'hooks.py', 'ledger.py', 'native/codex_queue_recovery.py'):
            path = source / 'murmur' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('owned runtime fixture ' + name)
            self.runtime_files[str(path.relative_to(self.release))] = hashlib.sha256(path.read_bytes()).hexdigest()
        readme = source / 'README.md'
        readme.write_text('unrelated product documentation')
        self.runtime_files[str(readme.relative_to(self.release))] = hashlib.sha256(readme.read_bytes()).hexdigest()
        manifest = self.release / 'release-manifest.json'
        manifest.write_text(json.dumps({'files': self.runtime_files, 'source_sha256': recovery._digest(self.runtime_files)}))
        self.python = self.root / 'python'
        self.python.write_bytes(b'owned interpreter fixture')
        self.launcher = self.root / 'murmur'
        self.launcher.write_bytes(recovery._launcher_bytes(source, self.python, self.state))
        self.installation = {'state': str(self.state), 'prefix': str(self.root),
            'launcher': {'path': str(self.launcher), 'sha256': hashlib.sha256(self.launcher.read_bytes()).hexdigest()},
            'runtime': {'mode': 'release', 'source': str(source), 'manifest': str(manifest),
                        'python': str(self.python), 'source_sha256': recovery._digest(self.runtime_files)}}
        (self.state / 'installation.json').write_text(json.dumps(self.installation))
        self.config, self.hooks = self.home / 'config.toml', self.home / 'hooks.json'
        self.config.write_text('model="fixture"\n')
        self.hooks.write_text('{"hooks":{}}')
        command = shlex.join(['/usr/bin/env', 'MURMUR_HOME=' + str(self.state), str(self.launcher),
                              'hook', 'codex', 'UserPromptSubmit']) + ' # murmur-mattermost-owned-v1'
        self.hook = {'key': str(self.hooks) + ':user_prompt_submit:1:0', 'eventName': 'userPromptSubmit',
                     'handlerType': 'command', 'command': command, 'source': 'user', 'sourcePath': str(self.hooks),
                     'enabled': True, 'async': False, 'matcher': None, 'timeoutSec': 8,
                     'trustStatus': 'trusted', 'currentHash': 'sha256:' + 'a' * 64}
        self.rpc = self.mock(recovery.codex, '_read_only_request', side_effect=lambda *a, **k: (
            {'data': [{'cwd': str(self.root), 'errors': [], 'warnings': [], 'hooks': [deepcopy(self.hook)]}]}, self.identity.target))
        self.mock(recovery, '__file__', new=str(source / 'murmur/native/codex_queue_recovery.py'))
        self.mock(recovery.sys, 'executable', new=str(self.python))

    def gate(self):
        return recovery._gate(self.identity, self.state, recovery.files.deadline(3))

    def upgrade(self, *, changes=None, remove=None):
        """A separate immutable release; preserve every archived original byte."""
        release = self.root / ('release-upgraded-' + str(len(list(self.root.glob('release-upgraded-*')))))
        release.mkdir()
        entries = dict(self.runtime_files)
        entries['tools/murmur/README.md'] = None  # A real metadata-only source SHA change.
        changes = {'tools/murmur/README.md': b'new documentation', **(changes or {})}
        for name in list(entries):
            if name == remove:
                del entries[name]
                continue
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(changes.get(name, (self.release / name).read_bytes()))
            entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for name, data in changes.items():
            if name not in entries:
                path = release / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                entries[name] = hashlib.sha256(data).hexdigest()
        manifest = release / 'release-manifest.json'
        manifest.write_text(json.dumps({'files': entries, 'source_sha256': recovery._digest(entries)}))
        source = release / 'tools/murmur'
        self.installation['runtime'].update(source=str(source), manifest=str(manifest),
                                            source_sha256=recovery._digest(entries))
        self.launcher.write_bytes(recovery._launcher_bytes(source, self.python, self.state))
        self.installation['launcher']['sha256'] = hashlib.sha256(self.launcher.read_bytes()).hexdigest()
        (self.state / 'installation.json').write_text(json.dumps(self.installation))
        recovery.__file__ = str(source / 'murmur/native/codex_queue_recovery.py')
        return self.gate()

    def equivalent(self, original, current):
        return recovery._gate_equivalence(original, current, recovery.files.deadline(3))

    def test_exact_trusted_hook_runtime_and_enrollment_proof_is_small_and_scoped(self):
        result = self.gate()
        self.assertEqual(result['ledger']['user_id'], 'account')
        self.assertEqual(result['native_hook_failure_policy'], 'fail_open')
        self.assertNotIn(str(self.config), result['files'])
        self.assertNotIn(str(self.hooks), result['files'])
        self.assertFalse(any(path.endswith('README.md') for path in result['files']))
        self.assertLess(len(json.dumps(result).encode()), 16_384)
        args = self.rpc.call_args
        self.assertEqual(args.args[1:3], ('hooks/list', {'cwds': [str(self.root)]}))

    def test_unrelated_configuration_edits_survive_but_owned_hook_or_critical_code_change_does_not(self):
        original = self.gate()
        self.config.write_text('model="another-model"\n')
        self.hooks.write_text('{"hooks":{"foreign":"edited"}}')
        self.assertEqual(self.gate(), original)
        self.hook['currentHash'] = 'sha256:' + 'b' * 64
        self.assertNotEqual(self.gate()['hook'], original['hook'])
        self.hook['command'] += ' changed'
        with self.assertRaisesRegex(NativeError, 'absent or ambiguous'):
            self.gate()
        self.hook['command'] = original['hook']['command']
        file = Path(self.installation['runtime']['source']) / 'murmur/hooks.py'
        file.write_text('modified actual Submit code')
        with self.assertRaisesRegex(NativeError, 'runtime file differs'):
            self.gate()

    def test_disabled_async_untrusted_or_concurrently_changed_configuration_is_unqualified(self):
        for name, value in (('enabled', False), ('async', True), ('trustStatus', 'untrusted'), ('matcher', 'only-some-prompts')):
            old = self.hook[name]
            self.hook[name] = value
            with self.subTest(field=name):
                with self.assertRaisesRegex(NativeError, 'trusted synchronous'):
                    self.gate()
            self.hook[name] = old
        previous = self.rpc.side_effect
        def change_while_listing(*args, **kwargs):
            result = previous(*args, **kwargs)
            self.config.write_text('changed while native config was resolving\n')
            return result
        self.rpc.side_effect = change_while_listing
        with self.assertRaisesRegex(NativeError, 'changed during native hook'):
            self.gate()

    def test_unresolved_native_config_errors_hold_but_unrelated_warnings_do_not_revoke_owned_hook(self):
        original = self.gate()
        previous = self.rpc.side_effect
        def warning(*args, **kwargs):
            result, target = previous(*args, **kwargs)
            result['data'][0]['warnings'] = ['unrelated optional plugin warning']
            return result, target
        self.rpc.side_effect = warning
        self.assertEqual(self.gate(), original)
        def error(*args, **kwargs):
            result, target = previous(*args, **kwargs)
            result['data'][0]['errors'] = [{'path': 'config.toml', 'message': 'configuration unresolved'}]
            return result, target
        self.rpc.side_effect = error
        with self.assertRaisesRegex(NativeError, 'configuration is unresolved'):
            self.gate()

    def test_v1_path_and_metadata_only_upgrade_preserves_original_proof(self):
        original = self.gate()
        frozen = deepcopy(original)
        current = self.upgrade()
        self.assertNotEqual(original, current)
        equivalent = self.equivalent(original, current)
        self.assertEqual(equivalent['kind'], 'codex-submit-release-equivalence-v1')
        self.assertEqual(equivalent['original_gate_sha256'], recovery._digest(original))
        self.assertNotEqual(equivalent['original_release_sha256'], equivalent['current_release_sha256'])
        self.assertEqual(original, frozen)
        self.assertIsNone(self.equivalent(current, current))

    def test_actual_gate_upgrade_retained_queue_and_final_recheck_preserve_original_attempt(self):
        token = 'A' * 32
        message = "You have new messages in 'Fixture'. [wake:" + token + ']'
        queue, history = self.home / 'queue_1.sqlite', self.home / 'thread_history_1.sqlite'
        with sqlite3.connect(queue) as c:
            c.execute('CREATE TABLE queued_items(id TEXT,thread_id TEXT,payload_json TEXT,queue_order INTEGER)')
        with sqlite3.connect(history) as c:
            c.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)')
            c.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)', (SESSION, 10, 1))
            c.execute('CREATE TABLE thread_items(thread_id TEXT,item_type TEXT,rollout_ordinal INTEGER,item_json TEXT)')
        emission = recovery.prepare_idle_handoff(self.identity, message, token, state_root=self.state)
        self.assertIsNotNone(emission)
        attempt = {'mode': 'idle', 'status': 'pending', 'token': token, 'emission': emission,
                   'native_identity': asdict(self.identity), 'evidence': {}}
        frozen = deepcopy(attempt)
        queue_id = '019a0000-0000-7000-8000-000000000001'
        payload = json.dumps({'UserInput': {'content': [{'type': 'text', 'text': message,
                                                       'text_elements': []}], 'client_id': 'correlation'}})
        with sqlite3.connect(queue) as c:
            c.execute('INSERT INTO queued_items VALUES(?,?,?,0)', (queue_id, SESSION, payload))
        queue_before = queue.read_bytes()
        self.upgrade(changes={recovery._RECOVERY_ONLY: b'recovery upgrade outside Submit closure'})
        successor = replace(self.identity, evidence={**self.identity.evidence, 'desktop_wake': {
            **self.descriptor, 'owner': {'pid': 401, 'birth': 'new backend'}}})
        self.discover.return_value = successor
        probe = recovery.probe_idle_recovery(self.identity, attempt, state_root=self.state)
        self.assertEqual(probe['status'], 'ready', probe)
        self.assertEqual(probe['queue_id'], queue_id)
        self.assertIsNotNone(probe['gate_equivalence'])
        with patch.object(recovery.desktop_wake, 'wake', return_value=DeliveryResult('accepted', 'URL scheduled; admission unconfirmed')) as wake:
            result = recovery.wake_idle_recovery(SESSION, probe, before_wake=lambda: True)
            self.assertEqual(result.status, 'accepted', result)
            self.assertIn('equivalent Submit install v1', result.detail)
            wake.assert_called_once()
            wake.reset_mock()
            self.hook['currentHash'] = 'sha256:' + 'b' * 64
            result = recovery.wake_idle_recovery(SESSION, probe,
                before_wake=lambda: self.fail('Changed owned hook reached callback'))
            self.assertEqual(result.status, 'uncertain')
            wake.assert_not_called()
        self.assertEqual(attempt, frozen)
        self.assertEqual(queue.read_bytes(), queue_before)

    def test_recovery_adapter_change_can_bootstrap_but_no_submit_dependency_or_set_change_can(self):
        original = self.gate()
        current = self.upgrade(changes={recovery._RECOVERY_ONLY: b'new recovery-only implementation'})
        self.assertIsNotNone(self.equivalent(original, current))
        for changes, remove in (({'tools/murmur/murmur/hooks.py': b'new Submit behavior'}, None),
                                ({'tools/murmur/murmur/ledger.py': b'new admission behavior'}, None),
                                ({'tools/murmur/murmur/__main__.py': b'import recovery during Submit'}, None),
                                ({'tools/murmur/murmur/new_dependency.py': b'new module'}, None),
                                ({}, 'tools/murmur/murmur/hooks.py')):
            with self.subTest(changes=changes, remove=remove):
                current = self.upgrade(changes=changes, remove=remove)
                with self.assertRaisesRegex(NativeError, 'dependency set or byte hashes'):
                    self.equivalent(original, current)

    def test_changed_interpreter_hook_or_enrollment_cannot_use_upgrade_equivalence(self):
        original = self.gate()
        current = self.upgrade()
        for section, field, value in (('hook', 'currentHash', 'sha256:' + 'b' * 64),
                                      ('hook', 'command', 'different owned command'),
                                      ('ledger', 'user_id', 'another-account'),
                                      ('ledger', 'enrollment_seq', 2),
                                      ('ledger', 'identity', [1, 2, 3, 4])):
            changed = deepcopy(current)
            changed[section][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(NativeError, 'mitigation/enrollment/ledger changed'):
                    self.equivalent(original, changed)
        self.python.write_bytes(b'changed actual interpreter')
        with self.assertRaisesRegex(NativeError, 'interpreter changed'):
            self.equivalent(original, self.gate())

    def test_incomplete_extra_mixed_root_or_unreconstructable_frozen_proof_holds(self):
        original = self.gate()
        current = self.upgrade()
        archived = str(self.release / 'tools/murmur/murmur/hooks.py')
        variants = []
        missing = deepcopy(original)
        del missing['files'][archived]
        variants.append(missing)
        extra = deepcopy(original)
        extra['files'][str(self.release / 'foreign.py')] = original['files'][archived]
        variants.append(extra)
        mixed = deepcopy(original)
        mixed['files'][str(self.root / 'other/tools/murmur/murmur/hooks.py')] = mixed['files'].pop(archived)
        variants.append(mixed)
        binding = deepcopy(original)
        binding['configuration']['installation_binding'] = 'wrong binding'
        variants.append(binding)
        unknown = deepcopy(original)
        unknown['configuration']['submit_policy'] = 'unknown additional constraint'
        variants.append(unknown)
        malformed = deepcopy(original)
        malformed['configuration'] = []
        variants.append(malformed)
        launcher = deepcopy(original)
        launcher['files'][str(self.launcher)]['sha256'] = 'a' * 64
        variants.append(launcher)
        for broken in variants:
            with self.subTest(proof=broken):
                with self.assertRaises(NativeError):
                    self.equivalent(broken, current)

    def test_archived_mutation_unsafe_files_and_noncanonical_launcher_hold(self):
        original = self.gate()
        current = self.upgrade()
        archive = self.release / recovery._RECOVERY_ONLY
        old_bytes = archive.read_bytes()
        archive.write_bytes(b'changed archived recovery module')
        with self.assertRaisesRegex(NativeError, 'Sealed Submit runtime changed'):
            self.equivalent(original, current)
        # Restoring bytes cannot restore the original inode timestamps/seal.
        archive.write_bytes(old_bytes)
        original = deepcopy(original)
        original['files'][str(archive)] = recovery._sealed_file(archive, recovery.files.deadline(3))[1]
        archive.chmod(0o666)
        with self.assertRaisesRegex(NativeError, 'safe owned native file'):
            self.equivalent(original, current)
        archive.chmod(0o644)
        original['files'][str(archive)] = recovery._sealed_file(archive, recovery.files.deadline(3))[1]
        self.launcher.write_bytes(self.launcher.read_bytes() + b'# unrelated executable change\n')
        self.installation['launcher']['sha256'] = hashlib.sha256(self.launcher.read_bytes()).hexdigest()
        (self.state / 'installation.json').write_text(json.dumps(self.installation))
        with self.assertRaisesRegex(NativeError, 'exact owned release template'):
            self.equivalent(original, self.gate())

    def test_manifest_race_holds_before_equivalence_is_offered(self):
        original = self.gate()
        current = self.upgrade()
        read = recovery._sealed_file
        manifest = self.release / 'release-manifest.json'
        def mutate_after_original_manifest(path, *args, **kwargs):
            result = read(path, *args, **kwargs)
            if Path(path) == manifest:
                manifest.write_text(manifest.read_text() + ' ')
            return result
        with patch.object(recovery, '_sealed_file', side_effect=mutate_after_original_manifest):
            with self.assertRaisesRegex(NativeError, 'Sealed Submit runtime changed'):
                self.equivalent(original, current)

    def test_installation_journal_race_holds_before_equivalence_is_offered(self):
        original = self.gate()
        current = self.upgrade()
        read = recovery._sealed_file
        journal = self.state / 'installation.json'
        changed = False
        def mutate_after_journal(path, *args, **kwargs):
            nonlocal changed
            result = read(path, *args, **kwargs)
            if Path(path) == journal and not changed:
                changed = True
                journal.write_text(journal.read_text() + ' ')
            return result
        with patch.object(recovery, '_sealed_file', side_effect=mutate_after_journal):
            with self.assertRaisesRegex(NativeError, 'installation changed during equivalence'):
                self.equivalent(original, current)
        self.assertTrue(changed)

    def test_runtime_mutation_after_its_byte_check_is_caught_before_publication(self):
        original = self.gate()
        current = self.upgrade()
        read = recovery._sealed_file
        archived = self.release / 'tools/murmur/murmur/hooks.py'
        changed = False
        def mutate_after_byte_check(path, *args, **kwargs):
            nonlocal changed
            result = read(path, *args, **kwargs)
            if Path(path) == archived and not changed:
                changed = True
                archived.write_bytes(b'late changed Submit code')
            return result
        with patch.object(recovery, '_sealed_file', side_effect=mutate_after_byte_check):
            with self.assertRaisesRegex(NativeError, 'runtime changed before equivalence publication'):
                self.equivalent(original, current)
        self.assertTrue(changed)


if __name__ == '__main__':
    unittest.main()
