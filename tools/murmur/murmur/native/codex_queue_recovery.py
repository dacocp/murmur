"""Qualified Desktop recipient refresh and retained-input wake-only recovery.

Fresh claims and already queued input stay separate. Recovery never enqueues,
deletes, settles, or retroactively attests a Submit gate. A trusted owned hook
mitigates repeated admission; the pinned runtime can fail open on hook failures.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import shlex
import sys
import time

from . import codex, context_receipts, desktop_wake, files
from .hooks import validate_doorbell
from .types import DeliveryResult, NativeError, NativeIdentity, validate_session_id


_OWNER_SHAS = frozenset({
    '58a91f6ea19bea477ae3a2ec48d619f0039afe75d3476260747e11d201fa7cea',
    '508388df35319042107560d095b271a98a512f59a542848c908b23ed1bad4b44',
    'a636331ed87605efc4cf84028ac2884a75363f9d10ead63cffb57ea0016d486e',
    '7cb8700a47be3c81bb3c5efbf2d2adc716ce3db4806e9dc992f2925f78b2b194',
    '3ea20787e106af94573d3d6c19480aed93a1334b0dd32b6b04e2f780ce82cf4b',
})
_KIND = 'codex-idle-handoff-v1'
_ORDINARY_SOURCE = {'cli': 'cli', 'vscode': 'vscode', 'exec': 'exec', 'appServer': 'mcp'}
_RECOVERY_ONLY = 'tools/murmur/murmur/native/codex_queue_recovery.py'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def _read_db(path, until):
    before = _file(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                  timeout=min(.2, files.remaining(until)))) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.set_progress_handler(lambda: time.monotonic() >= until, 1000)
        yield connection
        files.remaining(until)
    if _file(path) != before:
        raise NativeError('Native database identity changed during its read')


def _sealed_file(path, until, maximum=8 * 1024 * 1024):
    before = _file(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2] or opened.st_size > maximum:
            raise NativeError('Recovery gate file changed or exceeds its bound')
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        signature = lambda item: [item.st_size, item.st_mtime_ns, item.st_ctime_ns]
        if (len(data) != opened.st_size or signature(opened) != signature(after)
                or _file(path) != before):
            raise NativeError('Recovery gate file changed during inspection')
    files.remaining(until)
    return data, {'identity': list(before), 'stat': signature(opened),
                  'sha256': hashlib.sha256(data).hexdigest()}


def _base_identity(identity):
    return {key: value for key, value in asdict(identity).items() if key != 'evidence'}


def _capture(descriptor):
    return {name: descriptor[name] for name in
            ('schema', 'kind', 'app_bundle', 'app', 'owner', 'pipe', 'domain', 'startup_supported')}


def _installation_binding(installation):
    return _digest({'state': installation['state'], 'prefix': installation['prefix'],
                    'launcher': {key: installation['launcher'][key] for key in ('path', 'sha256')},
                    'runtime': {key: installation['runtime'][key] for key in
                                ('mode', 'python', 'source', 'manifest', 'source_sha256')}})


def _gate(identity, state_root, until):
    """Freeze a configured trusted mitigation, not native fail-closed assurance."""
    root = Path(state_root)
    if (not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise NativeError('Recovery gate requires the canonical private coordinator home')
    proofs = {}
    def read(path, maximum=8 * 1024 * 1024):
        data, proof = _sealed_file(Path(path), until, maximum)
        proofs[str(path)] = proof
        return data
    installation = context_receipts._decode(read(root / 'installation.json', 262144), until)
    if not isinstance(installation, dict):
        raise NativeError('Recovery gate ownership journal is malformed')
    native_home = Path(identity.evidence['desktop_wake']['domain']['codex_home'])
    hook_path, config_path = native_home / 'hooks.json', native_home / 'config.toml'
    read(hook_path, 262144)
    read(config_path, 2 * 1024 * 1024)
    if installation.get('state') != str(root) or installation.get('prefix') != str(native_home.parent):
        raise NativeError('Recovery gate installation targets another coordinator/native home')
    launcher = installation['launcher']
    read(Path(launcher['path']), 262144)
    if proofs[launcher['path']]['sha256'] != launcher['sha256']:
        raise NativeError('Recovery gate launcher differs from its ownership journal')
    runtime = installation['runtime']
    if runtime.get('mode') != 'release':
        raise NativeError('Restart qualification requires an owned immutable runtime release')
    source = Path(runtime['source'])
    if not source.is_absolute() or len(source.parents) < 2:
        raise NativeError('Recovery gate runtime source is not absolute')
    release_root = source.parents[1]
    if (source != release_root / 'tools/murmur' or Path(runtime['manifest']) != release_root / 'release-manifest.json'
            or Path(__file__).resolve() != source / 'murmur/native/codex_queue_recovery.py'
            or Path(sys.executable).resolve() != Path(runtime['python']).resolve()):
        raise NativeError('Recovery gate runtime is not this coordinator installation')
    read(Path(runtime['python']), 64 * 1024 * 1024)
    manifest = context_receipts._decode(read(Path(runtime['manifest']), 1024 * 1024), until)
    if not isinstance(manifest, dict):
        raise NativeError('Recovery gate release manifest is malformed')
    entries = manifest.get('files')
    if (not isinstance(entries, dict) or not 0 < len(entries) <= 4096
            or _digest(entries) != manifest.get('source_sha256')
            or manifest['source_sha256'] != runtime['source_sha256']):
        raise NativeError('Recovery gate runtime manifest is inconsistent')
    selected = {name: expected for name, expected in entries.items()
                if name.startswith('tools/murmur/murmur/') and name.endswith('.py')}
    if not selected:
        raise NativeError('Recovery gate release has no owned Python runtime')
    # The installer audits the complete release. This handoff binds only the
    # Python package imported by the Submit entrypoint, not tests/docs/UI/Go.
    for name, expected in selected.items():
        path = release_root / name
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or not path.is_relative_to(release_root)):
            raise NativeError('Recovery gate runtime path escapes its release')
        read(path)
        if proofs[str(path)]['sha256'] != expected:
            raise NativeError('Recovery gate runtime file differs from its release manifest')
    if str(Path(__file__).resolve().relative_to(release_root)) not in entries:
        raise NativeError('Recovery gate runtime manifest omits its adapter')
    ledger = root / 'operations.sqlite3'
    ledger_id = _file(ledger)
    with _read_db(ledger, until) as connection:
        row = connection.execute('''SELECT identity_json,mode,enrollment_id,enrollment_seq,user_id
            FROM actors WHERE actor=?''', (identity.actor,)).fetchone()
    if (not row or row[1] != 'native' or not isinstance(row[2], str) or not row[2]
            or type(row[3]) is not int or row[3] < 1 or not isinstance(row[4], str) or not row[4]):
        raise NativeError('Recovery gate has no exact native enrollment in this ledger')
    actual = NativeIdentity(**context_receipts._decode(row[0].encode(), until))
    if _base_identity(actual) != _base_identity(identity):
        raise NativeError('Recovery gate ledger targets another native recipient')
    command = shlex.join(['/usr/bin/env', 'MURMUR_HOME=' + str(root), launcher['path'],
                          'hook', 'codex', 'UserPromptSubmit']) + ' # murmur-mattermost-owned-v1'
    result, actual_target = codex._read_only_request(identity, 'hooks/list', {'cwds': [identity.target['cwd']]},
                                                     timeout=files.remaining(until))
    if actual_target != identity.target:
        raise NativeError('Native hook qualification changed its captured route')
    data = result.get('data') if isinstance(result, dict) else None
    if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
            or data[0].get('cwd') != identity.target['cwd'] or not isinstance(data[0].get('hooks'), list)
            or data[0].get('errors') != [] or not isinstance(data[0].get('warnings'), list)
            or len(data[0]['hooks']) > 4096 or any(not isinstance(entry, dict) for entry in data[0]['hooks'])):
        raise NativeError('Native effective hook configuration is unresolved')
    matches = [entry for entry in data[0].get('hooks', []) if entry.get('command') == command]
    if len(matches) != 1:
        raise NativeError('Native effective owned Submit hook is absent or ambiguous')
    hook = matches[0]
    if (hook.get('eventName') != 'userPromptSubmit' or hook.get('handlerType') != 'command'
            or hook.get('source') != 'user' or hook.get('sourcePath') != str(hook_path)
            or hook.get('enabled') is not True or hook.get('async', False) is not False
            or hook.get('matcher') is not None or hook.get('timeoutSec') != 8
            or hook.get('trustStatus') != 'trusted' or not isinstance(hook.get('currentHash'), str)
            or not hook['currentHash'].startswith('sha256:') or len(hook['currentHash']) != 71
            or any(c not in '0123456789abcdef' for c in hook['currentHash'][7:])
            or not hook.get('key', '').startswith(str(hook_path) + ':user_prompt_submit:')):
        raise NativeError('Native owned Submit hook is not the trusted synchronous mitigation')
    for name, expected in proofs.items():
        _, actual_proof = _sealed_file(Path(name), until, 64 * 1024 * 1024)
        if actual_proof != expected:
            raise NativeError('Recovery gate changed during native hook qualification')
    snapshots_only = {str(root / 'installation.json'), str(hook_path), str(config_path)}
    configuration = {'hooks_path': str(hook_path), 'config_path': str(config_path),
                     'installation_binding': _installation_binding(installation)}
    return {'ledger': {'path': str(ledger), 'identity': list(ledger_id),
                       'actor_binding_sha256': _digest(_base_identity(identity)),
                       'enrollment_id': row[2], 'enrollment_seq': row[3], 'user_id': row[4]},
            'hook': {name: hook[name] for name in ('key', 'currentHash', 'command', 'sourcePath')},
            'configuration': configuration,
            'files': {key: value for key, value in proofs.items() if key not in snapshots_only},
            'native_hook_failure_policy': 'fail_open'}


def _release_gate(gate, until):
    """Read the archived/current manifest without discarding its frozen seal."""
    proofs = gate['files']
    if not isinstance(proofs, dict) or not 0 < len(proofs) <= 4096:
        raise NativeError('Submit release proof has no bounded file set')
    manifests = [Path(name) for name in proofs if isinstance(name, str)
                 and Path(name).name == 'release-manifest.json']
    if len(manifests) != 1:
        raise NativeError('Submit release proof has no unique manifest')
    path = manifests[0]
    raw, proof = _sealed_file(path, until, 1024 * 1024)
    if proof != proofs[str(path)]:
        raise NativeError('Sealed Submit release manifest changed')
    manifest = context_receipts._decode(raw, until)
    entries = manifest.get('files') if isinstance(manifest, dict) else None
    if (not isinstance(entries, dict) or not 0 < len(entries) <= 4096
            or _digest(entries) != manifest.get('source_sha256')):
        raise NativeError('Sealed Submit release manifest is inconsistent')
    selected = {}
    for name, expected in entries.items():
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or str(Path(name)) != name or not isinstance(expected, str) or len(expected) != 64
                or any(char not in '0123456789abcdef' for char in expected)):
            raise NativeError('Sealed Submit release has an invalid file entry')
        if name.startswith('tools/murmur/murmur/') and name.endswith('.py'):
            selected[name] = expected
    if _RECOVERY_ONLY not in selected or len(selected) < 2:
        raise NativeError('Sealed Submit release omits the protected runtime')
    return path.parent, manifest, selected


def _launcher_bytes(source, python, state):
    # Exact installer template. An arbitrary shell-equivalent launcher is not
    # sufficient evidence that only its immutable release path changed.
    bootstrap = ('import runpy,sys;sys.path.insert(0,' + repr(str(source))
                 + ");runpy.run_module('murmur',run_name='__main__')")
    return ('#!/bin/sh\n# murmur-mattermost-owned-v1\n'
            'if [ -z "${MURMUR_HOME:-}" ]; then\n'
            + f'  MURMUR_HOME={shlex.quote(str(state))}\nfi\nexport MURMUR_HOME\n'
            + 'exec ' + shlex.join([str(python), '-c', bootstrap]) + ' "$@"\n').encode()


def _gate_equivalence(original, current, until):
    """Derive a separate v1 upgrade proof; never rewrite the original gate.

    Submit imports the package, CLI, hooks, coordinator and ledger. Only this
    module is outside that admission closure: native/__init__ loads it lazily
    for idle preparation/recovery, never for UserPromptSubmit. Requiring every
    other runtime file to remain byte-identical also protects that lazy-import
    boundary. New modules or changed admission dependencies require a hold.
    """
    if original == current:
        return None
    shape = {'ledger', 'hook', 'configuration', 'files', 'native_hook_failure_policy'}
    configuration = {'hooks_path', 'config_path', 'installation_binding'}
    if (any(not isinstance(gate, dict) or set(gate) != shape for gate in (original, current))
            or any(not isinstance(gate['configuration'], dict) or set(gate['configuration']) != configuration
                   for gate in (original, current))
            or any(original[key] != current[key] for key in ('ledger', 'hook', 'native_hook_failure_policy'))
            or any(original['configuration'][key] != current['configuration'][key]
                   for key in ('hooks_path', 'config_path'))):
        raise NativeError('Original owned Submit mitigation/enrollment/ledger changed')
    old_root, old_manifest, old_files = _release_gate(original, until)
    new_root, new_manifest, new_files = _release_gate(current, until)
    protected = {name: value for name, value in old_files.items() if name != _RECOVERY_ONLY}
    if (old_files.keys() != new_files.keys()
            or protected != {name: value for name, value in new_files.items() if name != _RECOVERY_ONLY}):
        raise NativeError('Submit admission dependency set or byte hashes changed')
    state = Path(current['ledger']['path']).parent
    journal_path = state / 'installation.json'
    raw, journal_proof = _sealed_file(journal_path, until, 262144)
    installation = context_receipts._decode(raw, until)
    runtime, launcher = installation['runtime'], installation['launcher']
    launcher_path, python = Path(launcher['path']), Path(runtime['python'])
    if (installation['state'] != str(state) or runtime['mode'] != 'release'
            or installation['prefix'] != str(Path(current['configuration']['config_path']).parent.parent)
            or runtime['source'] != str(new_root / 'tools/murmur')
            or runtime['manifest'] != str(new_root / 'release-manifest.json')
            or runtime['source_sha256'] != new_manifest['source_sha256']
            or _installation_binding(installation) != current['configuration']['installation_binding']):
        raise NativeError('Current Submit installation changed during equivalence inspection')
    for gate, root, entries in ((original, old_root, old_files), (current, new_root, new_files)):
        expected = {str(root / name) for name in entries} | {
            str(root / 'release-manifest.json'), str(launcher_path), str(python)}
        if set(gate['files']) != expected:
            raise NativeError('Submit release proof contains missing, extra or mixed-root files')
    if original['files'][str(python)] != current['files'][str(python)]:
        raise NativeError('Original Submit interpreter changed')
    launcher_data, launcher_proof = _sealed_file(launcher_path, until, 262144)
    if (launcher_proof != current['files'][str(launcher_path)]
            or launcher_data != _launcher_bytes(new_root / 'tools/murmur', python, state)):
        raise NativeError('Current Submit launcher is not the exact owned release template')
    old_launcher_sha = hashlib.sha256(_launcher_bytes(old_root / 'tools/murmur', python, state)).hexdigest()
    if original['files'][str(launcher_path)]['sha256'] != old_launcher_sha:
        raise NativeError('Frozen Submit launcher differs beyond its release path')
    archived_installation = {'state': installation['state'], 'prefix': installation['prefix'],
        'launcher': {'path': str(launcher_path), 'sha256': old_launcher_sha},
        'runtime': {'mode': 'release', 'python': str(python), 'source': str(old_root / 'tools/murmur'),
                    'manifest': str(old_root / 'release-manifest.json'),
                    'source_sha256': old_manifest['source_sha256']}}
    if _installation_binding(archived_installation) != original['configuration']['installation_binding']:
        raise NativeError('Frozen Submit installation binding cannot be reconstructed')
    # Check every archived byte, including the excluded recovery module. Its
    # *new* bytes may differ, but its original immutable proof must still hold.
    for gate, root, entries in ((original, old_root, old_files), (current, new_root, new_files)):
        for name, expected in gate['files'].items():
            if name == str(launcher_path):
                continue  # Reconstructed old launcher; current launcher checked above.
            _, actual = _sealed_file(Path(name), until, 64 * 1024 * 1024)
            if actual != expected:
                raise NativeError('Sealed Submit runtime changed during equivalence inspection')
        for name, expected in entries.items():
            if gate['files'][str(root / name)]['sha256'] != expected:
                raise NativeError('Sealed Submit runtime differs from its manifest')
    if (_sealed_file(journal_path, until, 262144)[1] != journal_proof
            or _sealed_file(launcher_path, until, 262144)[1] != launcher_proof):
        raise NativeError('Submit installation changed during equivalence inspection')
    expected_files = {name: proof for name, proof in original['files'].items() if name != str(launcher_path)}
    expected_files.update(current['files'])
    for name, proof in expected_files.items():
        path = Path(name)
        facts = path.lstat()
        if (list(_file(path)) != proof['identity']
                or [facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns] != proof['stat']):
            raise NativeError('Sealed Submit runtime changed before equivalence publication')
    files.remaining(until)
    return {'kind': 'codex-submit-release-equivalence-v1',
            'original_gate_sha256': _digest(original), 'current_gate_sha256': _digest(current),
            'original_release_sha256': old_manifest['source_sha256'],
            'current_release_sha256': new_manifest['source_sha256'],
            'protected_files_sha256': _digest(protected)}


def _queue(identity, text, token, until):
    home = Path(identity.evidence['desktop_wake']['domain']['codex_home'])
    path = home / 'queue_1.sqlite'
    file_id = _file(path)
    with _read_db(path, until) as connection:
        rows = connection.execute('''SELECT id,substr(payload_json,1,262145),length(payload_json)
            FROM queued_items WHERE thread_id=? AND instr(payload_json,?)>0
            ORDER BY queue_order LIMIT 3''', (identity.session_id, token)).fetchall()
    if len(rows) > 1:
        raise NativeError('Native queue has duplicate matching notification entries')
    if not rows:
        return {'path': str(path), 'identity': list(file_id)}, None
    queue_id, raw, length = rows[0]
    validate_session_id('codex', queue_id)
    if not isinstance(raw, str) or length > 262144 or len(raw.encode()) > 262144:
        raise NativeError('Native queued notification payload exceeds its bound')
    payload = context_receipts._decode(raw.encode(), until)
    user = payload.get('UserInput') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or set(payload) != {'UserInput'}
            or not isinstance(user, dict) or set(user) != {'content', 'client_id'}
            or user['content'] != [{'type': 'text', 'text': text, 'text_elements': []}]
            or not isinstance(user['client_id'], str) or not 0 < len(user['client_id']) <= 256):
        raise NativeError('Native queued notification token/body/type is contradictory')
    return {'path': str(path), 'identity': list(file_id)}, {
        'queue_id': queue_id, 'queue_payload_sha256': hashlib.sha256(raw.encode()).hexdigest(),
        'client_id': user['client_id']}


def _history(identity, token, client_id, until, *, floor=0):
    path = Path(identity.evidence['desktop_wake']['domain']['codex_home']) / 'thread_history_1.sqlite'
    file_id = _file(path)
    with _read_db(path, until) as connection:
        projection = connection.execute('''SELECT next_rollout_byte_offset,next_rollout_ordinal
            FROM thread_history_projection_state WHERE thread_id=?''', (identity.session_id,)).fetchone()
        if (not projection or any(type(value) is not int or value < 0 for value in projection)):
            raise NativeError('Native thread history projection is unresolved')
        # Presence is a conservative contradiction, not admission evidence. No
        # history body leaves SQLite and absence does not prove no model ran.
        match = connection.execute('''SELECT 1 FROM thread_items WHERE thread_id=?
            AND item_type='userMessage' AND rollout_ordinal>=?
            AND (instr(item_json,?)>0 OR instr(item_json,?)>0) LIMIT 1''',
                                   (identity.session_id, floor, token, client_id or token)).fetchone()
    if match:
        raise NativeError('Native history already contains notification/client correlation')
    return {'path': str(path), 'identity': list(file_id), 'projection': list(projection)}


def prepare_idle_handoff(identity, message, token, *, state_root, timeout=3):
    """Optional original qualification before the first possible enqueue.

    Lack of qualification leaves ordinary one-shot delivery intact. Positive
    preexisting queue/history contradictions stop that enqueue. Native Submit
    failures can still fail open; this is mitigation, not exactly-once proof.
    """
    validate_doorbell(message, token)
    until = files.deadline(timeout)
    try:
        candidate = prepare_idle_target(identity, timeout=files.remaining(until))
        descriptor = candidate.evidence.get('desktop_wake')
        if not descriptor or descriptor['verification']['profile']['owner_sha256'] not in _OWNER_SHAS:
            return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        # Unsupported evidence is not retrospective permission to recover.
        return None
    if _capture(descriptor) != _capture(identity.evidence['desktop_wake']):
        raise NativeError('Desktop owner changed after the original idle claim')
    queue_db, item = _queue(candidate, message, token, until)
    if item is not None:
        raise NativeError('Notification already exists before first enqueue')
    history_db = _history(candidate, token, None, until)
    gate = None
    try:
        gate = _gate(candidate, state_root, until)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return None
    return {'schema': 1, 'kind': _KIND, 'actor': identity.actor, 'session_id': identity.session_id,
            'token': token, 'text': message, 'gate': gate,
            'native': {'desktop': descriptor, 'thread': candidate.evidence['codex_idle_thread'],
                       'queue_database': queue_db, 'history_database': history_db, 'queue_was_absent': True}}


def probe_idle_recovery(identity, attempt, *, state_root, timeout=3):
    """Inspect one retained native row; never resend, settle or infer admission."""
    try:
        until = files.deadline(timeout)
        emission = attempt.get('emission') or {}
        if (attempt.get('mode') != 'idle' or attempt.get('status') != 'pending'
                or attempt.get('native_identity') != asdict(identity)
                or type(emission.get('schema')) is not int or emission['schema'] != 1 or emission.get('kind') != _KIND
                or emission.get('actor') != identity.actor or emission.get('session_id') != identity.session_id
                or emission.get('token') != attempt.get('token') or emission['native'].get('queue_was_absent') is not True):
            raise NativeError('Original idle handoff/recipient qualification is missing or changed')
        validate_doorbell(emission['text'], attempt['token'])
        if _capture(emission['native']['desktop']) != _capture(identity.evidence.get('desktop_wake')):
            raise NativeError('Original Desktop qualification differs from its frozen recipient')
        state = desktop_wake.owner_process_state(identity, timeout=files.remaining(until))
        if state.get('status') != 'ended':
            raise NativeError('Original Desktop backend has not positively ended')
        successor = desktop_wake.discover_successor(identity, timeout=files.remaining(until))
        descriptor = successor.evidence['desktop_wake']
        if (descriptor['domain'] != emission['native']['desktop']['domain']
                or descriptor['verification']['profile'] != emission['native']['desktop']['verification']['profile']):
            raise NativeError('Desktop successor source/profile/domain differs from the original handoff')
        thread = _persisted_thread(successor, descriptor, until)
        if thread != emission['native']['thread']:
            raise NativeError('Original native thread/source/history binding changed')
        gate = _gate(successor, state_root, until)
        equivalence = _gate_equivalence(emission['gate'], gate, until)
        queue_db, item = _queue(successor, emission['text'], attempt['token'], until)
        if queue_db != emission['native']['queue_database'] or item is None:
            raise NativeError('Original native queue entry is absent or its database changed')
        previous = (attempt.get('evidence') or {}).get('recovery') or {}
        if previous.get('queue_id') is not None and previous['queue_id'] != item['queue_id']:
            raise NativeError('Retained native queue ID differs from earlier recovery')
        if (previous.get('queue_payload_sha256') is not None
                and previous['queue_payload_sha256'] != item['queue_payload_sha256']):
            raise NativeError('Retained native queue payload differs from earlier recovery')
        original = emission['native']['history_database']
        history_db = _history(successor, attempt['token'], item['client_id'], until,
                              floor=original['projection'][1])
        if (history_db['path'] != original['path'] or history_db['identity'] != original['identity']
                or any(new < old for new, old in zip(history_db['projection'], original['projection']))):
            raise NativeError('Original native history database/projection changed')
        files.remaining(until)
        frozen = {key: deepcopy(attempt[key]) for key in ('mode', 'status', 'token', 'native_identity', 'emission')}
        frozen['evidence'] = {'recovery': {key: previous[key] for key in
            ('queue_id', 'queue_payload_sha256') if key in previous}}
        return {'status': 'ready', 'reason': 'One retained row; wake-only risk remains unconfirmed',
                **item, 'successor': descriptor,
                'gate_equivalence': equivalence,
                'proof': {'identity': asdict(identity), 'attempt': frozen, 'state_root': str(Path(state_root)),
                          'emission_sha256': _digest(emission)}}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        return {'status': 'held', 'reason': str(error)}


def wake_idle_recovery(session_id, probe, *, before_wake=None, timeout=3):
    """Freshly recheck the same row/gate/generation, then schedule a URL only."""
    try:
        until = files.deadline(timeout)
        inputs = probe['proof']
        identity = NativeIdentity(**inputs['identity'])
        if (probe.get('status') != 'ready' or session_id != identity.session_id
                or _digest(inputs['attempt'].get('emission')) != inputs['emission_sha256']):
            raise NativeError('Frozen wake-only probe changed')
        current = probe_idle_recovery(identity, inputs['attempt'], state_root=inputs['state_root'],
                                      timeout=files.remaining(until))
        if (current.get('status') != 'ready' or any(current.get(key) != probe.get(key)
                for key in ('queue_id', 'queue_payload_sha256', 'client_id', 'successor', 'gate_equivalence'))):
            raise NativeError('Native queue/gate/successor changed before wake-only delivery')
        if before_wake is not None and not before_wake():
            return DeliveryResult('rejected', 'Wake-only action changed after native recheck')
        result = desktop_wake.wake(session_id, current['successor'], timeout=files.remaining(until))
        equivalent = current.get('gate_equivalence')
        if equivalent is not None:
            detail = '; equivalent Submit install v1: ' + json.dumps({key: equivalent[key] for key in
                ('original_release_sha256', 'current_release_sha256', 'protected_files_sha256')},
                sort_keys=True, separators=(',', ':'))
            return replace(result, detail=result.detail + detail)
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        return DeliveryResult('uncertain', 'Original queued notification remains held: ' + str(error))


def _file(path):
    facts = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(facts.st_mode)
            or facts.st_uid != os.getuid() or facts.st_mode & 0o022):
        raise NativeError('Codex idle preparation requires a safe owned native file')
    return facts.st_dev, facts.st_ino, facts.st_uid, facts.st_mode


def _persisted_thread(identity, descriptor, until):
    """Read this exact thread only, preserving distinct API/SQLite provenance."""
    owner_sha = descriptor['verification']['profile']['owner_sha256']
    if owner_sha not in _OWNER_SHAS:
        raise NativeError('Codex idle recovery requires a qualified Desktop backend profile')
    source = codex.verified_thread_source(identity)
    if source is not None and (not isinstance(source, str) or source not in _ORDINARY_SOURCE):
        raise NativeError('Codex idle recovery requires a normal persisted Desktop thread source')
    home = Path(descriptor['domain']['codex_home'])
    database = home / 'state_5.sqlite'
    before = _file(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True,
                                  timeout=min(.2, files.remaining(until)))) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.set_progress_handler(lambda: time.monotonic() >= until, 1000)
        try:
            row = connection.execute('''SELECT id,substr(source,1,4097),archived,
                substr(rollout_path,1,4097),history_mode,substr(cwd,1,4097) FROM threads WHERE id=? LIMIT 2''',
                                     (identity.session_id,)).fetchall()
        finally:
            connection.set_progress_handler(None, 0)
    files.remaining(until)
    if _file(database) != before:
        raise NativeError('Codex native metadata database identity changed')
    if (len(row) != 1 or row[0][0] != identity.session_id or row[0][1] not in _ORDINARY_SOURCE.values()
            or row[0][2] != 0 or row[0][4] != 'paginated'):
        raise NativeError('Codex idle recovery requires an unarchived normal persisted Desktop thread')
    if source is not None and row[0][1] != _ORDINARY_SOURCE[source]:
        raise NativeError('Codex API/native database thread source disagrees')
    cwd = row[0][5]
    if (not isinstance(cwd, str) or not 0 < len(cwd) <= 4096 or not Path(cwd).is_absolute()
            or Path(cwd).resolve(strict=True) != Path(identity.target['cwd']).resolve(strict=True)):
        raise NativeError('Codex native thread cwd differs from its captured hook/queue route')
    value = row[0][3]
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or '\0' in value:
        raise NativeError('Codex idle thread has no bounded persisted rollout path')
    path, root = Path(value), home / 'sessions'
    if not path.is_absolute() or root.resolve(strict=True) != root or not path.is_relative_to(root):
        raise NativeError('Codex idle thread history lies outside its native session store')
    rollout = _file(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != rollout[:2]:
            raise NativeError('Codex idle history changed while opening')
        header = stream.readline(context_receipts._MAX_LINE + 1)
        after = os.fstat(stream.fileno())
        signature = lambda facts: (facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)
        if _file(path) != rollout or signature(opened) != signature(after):
            raise NativeError('Codex idle history pathname changed')
    digest, _ = context_receipts._header(header, identity, {
        'sha256': owner_sha, 'version': context_receipts.context_profile_version(owner_sha)}, until)
    return {'origin': 'native-state-db', 'session_id': identity.session_id, 'source': row[0][1],
            'cwd': str(Path(cwd).resolve(strict=True)),
            'database': {'path': str(database), 'identity': list(before)},
            'history': {'path': str(path), 'identity': list(rollout), 'header_sha256': digest}}


def prepare_idle_target(identity, *, timeout=3):
    """Verify the same actor's actual owner for a *new*, separately fenced claim.

    Current healthy legacy routes retain their ordinary one-shot behavior. Only
    the measured Desktop default-domain profiles may refresh a positively ended
    backend. Its native thread metadata and creation header are read without
    changing session/parent/target or inventing app-server source provenance.
    """
    try:
        until = files.deadline(timeout)
        if not isinstance(identity, NativeIdentity) or identity.provider != 'codex':
            raise NativeError('Expected a Codex native recipient')
        reason = codex.automatic_enrollment_reason(identity)
        if reason:
            raise NativeError(reason)
        if identity.evidence.get('desktop_wake') is None:
            return codex.verify_target(identity, timeout=files.remaining(until))
        try:
            descriptor = desktop_wake.verify(identity, timeout=files.remaining(until))
            candidate = replace(identity, evidence={**identity.evidence, 'desktop_wake': descriptor})
        except NativeError:
            state = desktop_wake.owner_process_state(identity, timeout=files.remaining(until))
            if state['status'] != 'ended':
                raise NativeError('Codex original Desktop backend has not verifiably ended')
            candidate = desktop_wake.discover_successor(identity, timeout=files.remaining(until))
            descriptor = candidate.evidence['desktop_wake']
        if descriptor['verification']['profile']['owner_sha256'] not in _OWNER_SHAS:
            if candidate.evidence['desktop_wake']['owner'] != identity.evidence['desktop_wake']['owner']:
                raise NativeError('Desktop successor backend is not a qualified idle profile')
            return candidate
        databases = desktop_wake._database_files(descriptor['owner'], descriptor['domain'], until)
        proof = _persisted_thread(candidate, descriptor, until)
        desktop_wake._recheck(descriptor, until)
        if any(desktop_wake._signature(Path(path)) != signature
               for path, signature in descriptor['verification']['files'].items()):
            raise NativeError('Codex Desktop source changed during idle preparation')
        if desktop_wake._database_files(descriptor['owner'], descriptor['domain'], until) != databases:
            raise NativeError('Codex Desktop native database generation changed')
        files.remaining(until)
        return replace(candidate, evidence={**candidate.evidence, 'codex_idle_thread': proof})
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        raise NativeError('Codex idle preparation is unavailable: ' + str(error)) from error
