#!/usr/bin/env python3
"""Agent-initiated human DM acceptance on an explicitly marked disposable server.

Uses real HTTP, metadata and message history with synthetic enrolled identities
and an isolated ledger. No native session, real operator or live installation is
selected. The fixture's contact setting is enabled; credentials are never printed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from murmur.backend import Backend, BackendError
from murmur.coordinator import Coordinator
from murmur.ledger import Ledger, LedgerError
from murmur.native import NativeIdentity
from server.fixture import fixture_arguments, load_fixture
from server.operator_integration_test import Server


def uid():
    return str(uuid.uuid4())


def run(fixture):
    server = Server(fixture.coordinator, fixture.admin)
    backend = Backend(fixture.coordinator['server_url'], fixture.coordinator['token'])
    profile = server.native('/users/me')
    assert profile['id'] == server.user_id and not profile.get('is_bot', False)
    original_username = profile['username']
    initial = server.native('/config')['PluginSettings']['Plugins'].get('murmur', {})
    sentinel = 'operator-fixture-' + uuid.uuid4().hex[:12]

    def contact(value):
        # Existing known secret comes from this protected fixture, never a masked GET.
        current = server.native('/config')['PluginSettings']['Plugins'].get('murmur', {})
        desired = {**current, 'coordinatortoken': backend.token,
                   'operatoruserid': value, 'operator_fixture_option': sentinel}
        server.native('/config/patch', {'PluginSettings': {'Plugins': {'murmur': desired}}}, method='PUT')
        observed = server.native('/config')['PluginSettings']['Plugins']['murmur']
        assert observed['operatoruserid'] == value and observed['operator_fixture_option'] == sentinel
        backend.request('GET', '/health')  # Config update retained the actual bearer.

    with tempfile.TemporaryDirectory(prefix='murmur-operator-http-') as directory:
        ledger = Ledger(Path(directory) / 'operations.sqlite3')
        runtime = Coordinator(ledger, backend)
        try:
            a = NativeIdentity('codex', uid(), parent_known=True)
            b = NativeIdentity('codex', uid(), parent_known=True)
            child = NativeIdentity('codex', uid(), parent_session_id=a.session_id, parent_known=True)
            d = NativeIdentity('codex', uid(), parent_known=True)
            for identity in (a, b, child, d):
                runtime.register(identity, 'Operator HTTP ' + identity.session_id[:8], inherit='fresh')
            actor = a.key
            contact('')
            try:
                runtime.mutate(actor, 'operator_dm')
                raise AssertionError('Unconfigured contact must fail')
            except BackendError as error:
                assert error.status == 403 and not error.uncertain
            contact(server.user_id)
            result = runtime.mutate(actor, 'operator_dm')['conversation']
            dm = result['id']
            assert set(result) == {'id', 'name', 'kind'} and result['kind'] == 'dm'
            assert runtime.mutate(actor, 'operator_dm')['conversation']['id'] == dm
            bot = ledger.actor(actor)['user_id']
            members = server.native('/channels/' + dm + '/members')
            assert {m['user_id'] for m in members} == {server.user_id, bot} and len(members) == 2
            runtime.sync(actor)
            assert runtime.inbox(actor) == [] and ledger.attempts(actor) == []
            empty = server.native('/channels/' + dm + '/posts')
            assert not [p for p in empty['posts'].values() if not p.get('type', '').startswith('system_')]
            before_unread = server.native('/users/' + server.user_id + '/channels/' + dm + '/unread')
            assert before_unread['msg_count'] == 0
            first = runtime.mutate(actor, 'post', channel_id=dm, message='Agent starts the conversation')['post']
            assert first['user_id'] == bot and not first['root_id']
            native = server.native('/channels/' + dm + '/posts')
            assert native['posts'][first['id']]['user_id'] == bot
            unread = server.native('/users/' + server.user_id + '/channels/' + dm + '/unread')
            assert unread['msg_count'] >= 1
            answer = server.native('/posts', {'channel_id': dm, 'root_id': first['id'],
                                             'message': 'Human can reply normally'}, expected=201)
            deadline = time.monotonic() + 10
            while True:
                runtime.sync(actor)
                pending = ledger.unread_posts(actor)
                if any(p['id'] == answer['id'] for p in pending):
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError('Canonical human reply did not reach the ordinary feed')
                time.sleep(0.1)
            assert [p['id'] for p in pending] == [answer['id']] and pending[0]['notification_eligible']
            ledger.set_mute(actor, dm, True)
            runtime.mutate(actor, 'operator_dm')
            runtime.sync(actor)
            assert next(c for c in ledger.conversations(actor) if c['id'] == dm)['mute_explicit'] == 1
            assert next(c for c in ledger.conversations(actor) if c['id'] == dm)['muted'] == 1

            # An inherited reference stays read-only; the child opens its own pair.
            runtime.sync(child.key)
            try:
                runtime.mutate(child.key, 'post', channel_id=dm, message='Forbidden inherited write')
                raise AssertionError('Observer must not post')
            except LedgerError as error:
                assert 'full participation' in str(error)
            own = runtime.mutate(child.key, 'operator_dm')['conversation']['id']
            assert own != dm

            # A human may start a native DM before any Murmur open command.
            # The configured watch proved this pair absent, so its first post is
            # fresh; a later explicit open must reuse it without rebasing mail.
            native_pair = server.native('/channels/direct', [server.user_id, ledger.actor(b.key)['user_id']], expected=201)
            first_human = server.native('/posts', {'channel_id': native_pair['id'],
                'message': 'Human starts a native DM before any Murmur open'}, expected=201)
            deadline = time.monotonic() + 15
            while True:
                runtime.sync(b.key)
                first_receipt = next((p for p in ledger.unread_posts(b.key) if p['id'] == first_human['id']), None)
                if first_receipt is not None:
                    break
                assert time.monotonic() < deadline, 'First native human DM did not reach the ordinary recipient feed'
                time.sleep(0.1)
            assert first_receipt['notification_eligible'] and first_receipt['announced_at'] is None
            receipt_fields = ('first_seen_seq', 'notification_eligible', 'read_at', 'announced_at')
            before_open = {key: first_receipt[key] for key in receipt_fields}
            adopted = runtime.mutate(b.key, 'operator_dm')['conversation']['id']
            assert adopted == native_pair['id']
            runtime.sync(b.key)
            after_open = next(p for p in ledger.unread_posts(b.key) if p['id'] == first_human['id'])
            assert {key: after_open[key] for key in receipt_fields} == before_open

            # Real simultaneous HTTP requests in both directions converge.
            operation_id = uid()
            with ThreadPoolExecutor(max_workers=2) as pool:
                agent = pool.submit(backend.mutate, 'operator_dm', d.key, operation_id)
                human = pool.submit(server.operator, '/dms', {'op_id': uid(), 'agent_id': d.key})
                concurrent = agent.result()['conversation']['id']
                assert human.result()['conversation']['id'] == concurrent
            assert backend.operation(d.key, operation_id) == {'status': 'complete', 'result': {'channel_id': concurrent}}
            try:
                backend.operation(actor, operation_id)
                raise AssertionError('Another actor must not inspect the operation')
            except BackendError as error:
                assert error.status == 403
            server.operator('/operations/' + operation_id, expected=403)

            # The same configured account remains the target after a rename.
            server.native('/users/' + server.user_id + '/patch', {'username': 'operator-' + uuid.uuid4().hex[:12]}, method='PUT')
            assert runtime.mutate(actor, 'operator_dm')['conversation']['id'] == dm
            assert backend.operation(d.key, operation_id)['result']['channel_id'] == concurrent
            return {'new_pair': dm, 'first_agent_post': first['id'], 'human_reply': answer['id'],
                    'human_native_unread': unread['msg_count'], 'child_pair': own,
                    'native_first_post_pair': adopted, 'native_first_post': first_human['id'],
                    'native_first_post_eligible': True, 'concurrent_pair': concurrent,
                    'initial_contact_disabled': not initial.get('operatoruserid'),
                    'native_sessions_started': 0}
        finally:
            # Keep bootstrap's fixture login usable; retain enabled contact for rerun checks.
            server.native('/users/' + server.user_id + '/patch', {'username': original_username}, method='PUT')
            ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Run only on the selected disposable installation')
    fixture_arguments(parser)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run and an explicitly marked disposable installation are required')
    fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
    print(json.dumps(run(fixture), indent=2))


if __name__ == '__main__':
    main()
