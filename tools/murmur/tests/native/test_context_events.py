"""Synthetic event-admission proofs; never open native histories or processes."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import unittest
from unittest.mock import patch

from murmur import native
from murmur.native import context_receipts as receipts
from murmur.native.types import NativeError
from . import test_context_receipts as fixtures

SHA_ALPHA6 = fixtures.SHA_ALPHA6
SHA_0166 = fixtures.SHA_0166
SHA_0167 = fixtures.SHA_0167
SHA_0167_ALPHA4 = fixtures.SHA_0167_ALPHA4
stored_schema1_capture = fixtures.stored_schema1_capture


KIND = 'codex-canonical-event-jsonl'
TRIPWIRE = 'UNRELATED_HISTORY_DECODER_TRIPWIRE'
MIB = 1024 * 1024

# Each invocation imports a fresh reader. Count its real source IO, including
# guard rereads, and refuse decoding any earlier unrelated historical record.
COLD_READER = r'''
from dataclasses import asdict
import json,os,sys
from unittest.mock import patch
from murmur import native
from murmur.native import context_receipts as reader
from murmur.native.types import HookEvent,NativeIdentity
c=json.load(sys.stdin)
identity=NativeIdentity(**c['captured'].pop('identity'))
captured=HookEvent(identity=identity,**c['captured'])
facts=os.stat(captured.transcript_path)
pread=reader.os.pread; decode=reader._decode
metrics={'bytes_read':0,'ranges':[],'decoded_records':0}
def counted(fd,size,offset):
    value=pread(fd,size,offset)
    seen=os.fstat(fd)
    if (seen.st_dev,seen.st_ino)==(facts.st_dev,facts.st_ino):
        metrics['bytes_read']+=len(value)
        metrics['ranges'].append([offset,len(value)])
    return value
def checked(line,deadline):
    if b'UNRELATED_HISTORY_DECODER_TRIPWIRE' in bytes(line):
        raise AssertionError('Reader decoded an unrelated historical body')
    metrics['decoded_records']+=1
    return decode(line,deadline)
with patch.object(reader,'_owner_profile',return_value=c['profile']), \
     patch.object(reader.os,'pread',side_effect=counted), \
     patch.object(reader,'_decode',side_effect=checked):
    value=native.prepare_context_receipt(captured) if c['mode']=='capture' else native.verify_context_receipt(identity,c['attempt'],timeout=.5)
print(json.dumps({'value':value,'metrics':metrics}))
'''


class EventReceiptTests(unittest.TestCase):
    reset_stream = fixtures.ContextReceiptTests.reset_stream
    encode = staticmethod(fixtures.ContextReceiptTests.encode)
    append = fixtures.ContextReceiptTests.append
    context_row = fixtures.ContextReceiptTests.context_row
    compacted_row = fixtures.ContextReceiptTests.compacted_row
    verify = fixtures.ContextReceiptTests.verify
    assert_unknown = fixtures.ContextReceiptTests.assert_unknown

    def setUp(self):
        fixtures.ContextReceiptTests.setUp(self)
        self.profile.update(version='0.166.0-alpha.6', sha256=SHA_ALPHA6)
        self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)

    def offer(self, request):
        # This models the existing hook→ledger order: the watermark is stable
        # before claim_context mints the fresh unpredictable notification token.
        self.token = secrets.token_urlsafe(24)
        self.message = native.format_doorbell(['synthetic event test'], self.token)
        return {'actor': self.identity.actor, 'token': self.token,
                'expected_turn_key': self.captured.turn_key, 'tool_id': self.tool_id,
                'receipt_request': deepcopy(request), 'emission': {'context': self.message}}

    def attempt(self):
        request = native.prepare_context_receipt(self.captured)
        self.assertEqual((request['schema'], request['kind']), (2, KIND))
        self.assertNotIn('prefix_sha256', request)
        self.assertNotIn('line_count', request)
        return self.offer(request)

    def next_row(self, attempt, *, step=1):
        return self.context_row(ordinal=attempt['receipt_request']['anchor_ordinal'] + step)

    def historical_prefix(self, rows=1, padding=128 * 1024):
        header = json.loads(self.path.read_bytes().splitlines()[0])
        with self.path.open('wb') as stream:
            stream.write(self.encode(header))
            for number in range(rows):
                stream.write(self.encode({'type': 'response_item', 'ordinal': 8922 + number,
                    'payload': {'type': 'message', 'role': 'assistant', 'content': TRIPWIRE + 'a' * padding}}))
            stream.write(self.encode({'type': 'event_msg', 'ordinal': 8922 + rows,
                'payload': {'type': 'task_started', 'turn_id': self.turn_id}}))
        self.path.chmod(0o600)

    def cold(self, mode, attempt=None):
        settings = {'mode': mode, 'captured': asdict(self.captured), 'profile': self.profile, 'attempt': attempt}
        package = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=package, PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.run([sys.executable, '-c', COLD_READER], input=json.dumps(settings),
            capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_capture_freezes_header_complete_last_record_and_eof_before_nonce(self):
        before = self.path.read_bytes()
        last = before.splitlines(keepends=True)[-1]
        request = native.prepare_context_receipt(self.captured)
        attempt = self.offer(request)
        self.assertEqual(request['offset'], len(before))
        self.assertEqual(request['anchor_start'], len(before) - len(last))
        self.assertEqual(request['anchor_sha256'], hashlib.sha256(last).hexdigest())
        self.assertEqual(request['ordinal_base'], 8921)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(attempt['token'], json.dumps(request))
        self.assert_unknown(self.verify(attempt))
        row = self.next_row(attempt)
        self.append(row)
        result = self.verify(attempt)
        self.assertEqual(result['status'], 'accepted', result)
        evidence = result['evidence']
        self.assertEqual((evidence['source'], evidence['byte_start'], evidence['byte_end']),
                         (KIND, len(before), self.path.stat().st_size))
        self.assertEqual((evidence['message_id'], evidence['turn_key'], evidence['tool_id']),
                         (row['payload']['id'], self.captured.turn_key, self.tool_id))

    def test_outside_anchor_mutation_is_allowed_only_by_new_event_contract(self):
        self.historical_prefix()
        old = stored_schema1_capture(self.captured, self.profile)
        request = native.prepare_context_receipt(self.captured)
        attempt = self.offer(request)
        legacy = deepcopy(attempt)
        legacy['receipt_request'] = old
        data = self.path.read_bytes()
        changed = data.replace(TRIPWIRE.encode(), b'X' * len(TRIPWIRE), 1)
        self.assertEqual(len(changed), len(data))
        self.assertEqual(changed[request['anchor_start']:], data[request['anchor_start']:])
        self.path.write_bytes(changed)
        self.append(self.next_row(attempt))
        self.assertEqual(self.verify(attempt)['status'], 'accepted')
        self.assert_unknown(self.verify(legacy))  # Frozen schema1/508 is never weakened.

    def test_old_schema1_capture_with_current_writer_remains_readable_and_full_prefix_bound(self):
        request = stored_schema1_capture(self.captured, self.profile)
        attempt = self.offer(request)
        self.append(self.context_row(ordinal=8923))
        result = self.verify(attempt)
        self.assertEqual((result['status'], result['evidence']['source']), ('accepted', 'codex-canonical-jsonl'))

    def test_event_format_is_not_available_to_another_or_unknown_current_writer(self):
        attempt = self.attempt()
        self.append(self.next_row(attempt))
        for profile in ({**self.profile, 'sha256': SHA_0166, 'version': '0.166.0-alpha.5.1'},
                        {**self.profile, 'sha256': 'f' * 64}, {**self.profile, 'version': 'other'}):
            changed = deepcopy(attempt)
            changed['receipt_request']['profile'] = profile
            with self.subTest(profile=profile):
                self.assert_unknown(self.verify(changed))
        self.profile.update(sha256=SHA_0166, version='0.166.0-alpha.5.1')
        self.reset_stream(header={'cli_version': '0.164.0-alpha.11.1'})
        self.assertEqual(native.prepare_context_receipt(self.captured)['schema'], 1)

    def test_0167_exact_writer_keeps_event_contract_and_frozen_older_profile(self):
        self.assert_qualified_writer(SHA_0167, '0.167.0-alpha.3.2')

    def test_0167_alpha4_writer_keeps_event_contract_and_frozen_older_profile(self):
        self.assert_qualified_writer(SHA_0167_ALPHA4, '0.167.0-alpha.4.1')

    def assert_qualified_writer(self, sha256, version):
        previous = self.attempt()
        self.append(self.next_row(previous))
        self.profile.update(sha256=sha256, version=version)
        self.assertEqual(self.verify(previous)['evidence']['profile']['sha256'], SHA_ALPHA6)

        self.reset_stream(header={'cli_version': '0.164.0-alpha.11.1'}, ordinal_base=19066)
        attempt = self.attempt()
        self.assertEqual(attempt['receipt_request']['profile'], self.profile)
        self.assertEqual(attempt['receipt_request']['ordinal_base'], 19066)
        with patch.object(receipts, '_snapshot', side_effect=AssertionError('No full-history fallback')):
            history = receipts.inspect_context_history(self.identity, self.profile, str(self.path))
        self.assertEqual(history['status'], 'supported', history)
        row = self.next_row(attempt)
        self.append(row)
        frozen, before = deepcopy(attempt), self.path.read_bytes()
        result = self.verify(attempt)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(result['evidence']['profile']['sha256'], sha256)
        self.assertEqual(result['evidence']['source'], KIND)
        self.assertEqual(result['evidence']['ordinal'], 19068)
        self.assertEqual((attempt, self.path.read_bytes()), (frozen, before))
        for sha, version in ((sha256, '0.166.0-alpha.6'),
                             ('f' * 64, version), (SHA_ALPHA6, version)):
            changed = deepcopy(attempt)
            changed['receipt_request']['profile'].update(sha256=sha, version=version)
            with self.subTest(sha=sha, version=version):
                self.assert_unknown(self.verify(changed))

    def test_recipient_turn_tool_token_and_modern_provenance_remain_mandatory(self):
        attempt = self.attempt()
        row = self.next_row(attempt)
        self.append(row)
        for field in ('actor', 'expected_turn_key', 'tool_id', 'token'):
            changed = deepcopy(attempt); changed[field] = 'other'
            with self.subTest(field=field):
                self.assert_unknown(self.verify(changed))
        child = replace(self.identity, session_id='33333333-3333-4333-8333-333333333333')
        self.assert_unknown(self.verify(attempt, child))
        for provenance in (None, {'type': 'harness', 'hook': False}, {'type': 'harness', 'hook': 1},
                           {'type': 'client', 'hook': True}):
            self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
            attempt = self.attempt(); row = self.next_row(attempt)
            metadata = row['payload']['internal_chat_message_metadata_passthrough']
            if provenance is None:
                metadata.pop('content_item_metadata')
            else:
                metadata['content_item_metadata'] = [{'provenance': provenance}]
            self.append(row)
            with self.subTest(provenance=provenance):
                self.assert_unknown(self.verify(attempt))

    def test_historical_quoted_nested_or_replayed_old_nonce_never_supplies_new_admission(self):
        old_row = self.context_row(ordinal=8923)
        self.append(old_row)
        attempt = self.attempt()
        self.assert_unknown(self.verify(attempt))
        replay = deepcopy(old_row); replay['ordinal'] = 8924
        self.append(replay)
        self.assert_unknown(self.verify(attempt))
        for number, role in enumerate(('user', 'assistant', 'tool'), 8925):
            quote = self.context_row(ordinal=number); quote['payload']['role'] = role
            self.append(quote)
        self.append(self.compacted_row(ordinal=8928, padding=64))
        self.assert_unknown(self.verify(attempt))
        self.append(self.context_row(ordinal=8929))
        self.assertEqual(self.verify(attempt)['status'], 'accepted')

    def test_compaction_can_be_anchor_but_only_a_new_outer_context_qualifies(self):
        self.append(self.compacted_row(ordinal=8923, padding=4096))
        attempt = self.attempt()
        self.assertEqual(attempt['receipt_request']['anchor_ordinal'], 8923)
        self.append(self.compacted_row(ordinal=8924, padding=4096))
        self.assert_unknown(self.verify(attempt))
        self.append(self.context_row(ordinal=8925))
        self.assertEqual(self.verify(attempt)['status'], 'accepted')

    def test_changed_header_anchor_inode_or_observed_truncation_invalidates_offer(self):
        for change in ('header', 'anchor', 'inode', 'truncate'):
            self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
            attempt = self.attempt()
            if change == 'inode':
                data = self.path.read_bytes(); self.path.rename(self.path.with_suffix('.old')); self.path.write_bytes(data)
            elif change == 'truncate':
                self.path.write_bytes(self.path.read_bytes()[:attempt['receipt_request']['anchor_start']])
            else:
                data = self.path.read_bytes().replace(b'0.162.0-alpha.2', b'0.162.0-alpha.3') if change == 'header' else self.path.read_bytes().replace(b'task_started', b'task_changed')
                self.path.write_bytes(data)
            self.path.chmod(0o600)
            self.append(self.next_row(attempt))
            with self.subTest(change=change):
                self.assert_unknown(self.verify(attempt))

    def test_partial_oversized_or_malformed_capture_boundary_never_yields_watermark(self):
        variants = (b'{"type":"event_msg"', b'\n', self.encode({'type': 'event_msg', 'ordinal': True}),
                    self.encode({'type': 'event_msg', 'ordinal': 8920}), b'x' * (8 * MIB + 1) + b'\n')
        for row in variants:
            self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
            self.append(row)
            with self.subTest(size=len(row)), self.assertRaises(NativeError):
                native.prepare_context_receipt(self.captured)
        self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
        rows = [json.loads(row) for row in self.path.read_bytes().splitlines()]
        rows[0]['payload']['padding'] = 'x' * (8 * MIB)
        self.path.write_bytes(b''.join(self.encode(row) for row in rows))
        with self.assertRaises(NativeError):
            native.prepare_context_receipt(self.captured)

    def test_malformed_sealed_offsets_and_identity_cannot_redirect_event_verification(self):
        attempt = self.attempt(); self.append(self.next_row(attempt))
        changes = ({'offset': True}, {'offset': -1}, {'offset': 2 ** 63}, {'offset': '2'},
                   {'anchor_start': -1}, {'anchor_start': True}, {'anchor_start': attempt['receipt_request']['offset']},
                   {'anchor_ordinal': True}, {'ordinal_base': -1}, {'inode': 0}, {'device': -1},
                   {'header_sha256': '0' * 64}, {'anchor_sha256': '0' * 64}, {'session_id': 'other'},
                   {'kind': 'codex-canonical-jsonl'}, {'schema': 1})
        for change in changes:
            modified = deepcopy(attempt); modified['receipt_request'].update(change)
            with self.subTest(change=change):
                self.assert_unknown(self.verify(modified))

    def test_partial_or_invalid_new_rows_hold_without_skipping_to_a_later_match(self):
        deep = 'leaf'
        for _ in range(130):
            deep = [deep]
        bad_rows = (b'{not-json}\n', self.encode({'type': 'event_msg', 'ordinal': 8923, 'ordinal_duplicate': True}).replace(b'"ordinal_duplicate":true', b'"ordinal":8923'),
                    self.encode({'type': 'event_msg', 'ordinal': 99}), self.encode({'type': 'event_msg', 'ordinal': True}),
                    self.encode({'type': 'event_msg', 'ordinal': 8923, 'payload': {'number': float('nan')}}),
                    self.encode({'type': 'event_msg', 'ordinal': 8923, 'payload': {'padding': deep}}),
                    self.encode({'type': 'event_msg', 'ordinal': 8923, 'payload': {'padding': 'x' * (8 * MIB)}}))
        for bad in bad_rows:
            self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
            attempt = self.attempt(); self.append(bad); self.append(self.context_row(ordinal=8924))
            with self.subTest(bad=bad[:60]):
                self.assert_unknown(self.verify(attempt))
        self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
        attempt = self.attempt(); row = self.encode(self.next_row(attempt))
        self.append(row[:-1]); self.assert_unknown(self.verify(attempt))
        self.append(b'\n'); self.assertEqual(self.verify(attempt)['status'], 'accepted')

    def test_first_exact_receipt_survives_unrelated_partial_or_oversized_later_suffix(self):
        attempt = self.attempt(); self.append(self.next_row(attempt))
        first = self.verify(attempt)
        self.assertEqual(first['status'], 'accepted')
        self.append(b'{partial' + b'x' * (17 * MIB))
        self.assertEqual(self.verify(attempt), first)

    def test_concurrent_append_is_allowed_but_changed_scanned_bytes_or_unsafe_mode_are_not(self):
        for change in ('append', 'matching-bytes', 'mode'):
            self.reset_stream(header={'cli_version': '0.162.0-alpha.2'}, ordinal_base=8921)
            attempt = self.attempt(); self.append(self.next_row(attempt))
            original = receipts.os.pread
            exercised = []

            def race(fd, size, offset):
                value = original(fd, size, offset)
                if not exercised and offset >= attempt['receipt_request']['offset'] and self.token.encode() in value:
                    exercised.append(change)
                    if change == 'append':
                        self.append({'type': 'event_msg', 'ordinal': 8924, 'payload': {}})
                    elif change == 'mode':
                        self.path.chmod(0o666)
                    else:
                        position = offset + value.index(self.token.encode())
                        with self.path.open('r+b') as stream:
                            stream.seek(position)
                            stream.write(b'Y' if self.token.startswith('X') else b'X')
                return value

            with self.subTest(change=change), patch.object(receipts.os, 'pread', side_effect=race):
                result = self.verify(attempt)
                self.assertEqual(exercised, [change], 'The intended read/write interleaving must occur')
                if change == 'append':
                    self.assertEqual(result['status'], 'accepted', result)
                else:
                    self.assert_unknown(result)
            self.path.chmod(0o600)

    def test_scan_exhaustion_is_unknown_even_if_match_exists_after_the_bound(self):
        attempt = self.attempt()
        for ordinal in range(8923, 8940):
            self.append({'type': 'event_msg', 'ordinal': ordinal, 'payload': {'padding': 'x' * MIB}})
        self.append(self.context_row(ordinal=8940))
        result = self.verify(attempt)
        self.assert_unknown(result)
        self.assertTrue(any(word in result['reason'].lower() for word in ('bound', 'limit', 'scan')), result)

    def test_repeat_verification_and_reopened_reader_are_readonly_and_keep_frozen_owner(self):
        attempt = self.attempt(); self.append(self.next_row(attempt))
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns, sorted(self.root.rglob('*'))
        result = self.verify(attempt)
        with patch.object(receipts, '_owner_profile', side_effect=NativeError('owner exited')):
            self.assertEqual(self.verify(attempt), result)
        restarted = self.cold('verify', attempt)
        self.assertEqual(restarted['value'], result)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns, sorted(self.root.rglob('*'))), before)

    def test_event_source_can_exceed_legacy_total_size_without_certifying_unread_middle(self):
        header = self.path.read_bytes().splitlines(keepends=True)[0]
        with self.path.open('wb') as stream:
            stream.write(header)
            stream.seek(300 * MIB)
            stream.write(b'\n' + self.encode({'type': 'event_msg', 'ordinal': 999999,
                'payload': {'type': 'task_started', 'turn_id': self.turn_id}}))
        # The sparse middle is deliberately opaque. V2 proves this future event,
        # not valid old history or continuity from the creation header to EOF.
        attempt = self.attempt(); self.assertGreater(attempt['receipt_request']['offset'], 256 * MIB)
        self.append(self.next_row(attempt))
        self.assertEqual(self.verify(attempt)['status'], 'accepted')

    @unittest.skipUnless(os.environ.get('MURMUR_LARGE_HISTORY_TEST') == '1', 'opt-in 180 MiB fresh-process IO proof')
    def test_fresh_capture_and_verifier_read_bounded_bytes_on_small_and_180_mib_prefixes(self):
        samples = []
        for rows in (1, 180):
            self.historical_prefix(rows=rows, padding=MIB)
            captured = self.cold('capture')
            request = captured['value']; self.assertEqual((request['schema'], request['kind']), (2, KIND))
            attempt = self.offer(request); self.append(self.next_row(attempt))
            verified = self.cold('verify', attempt)
            self.assertEqual(verified['value']['status'], 'accepted', verified)
            # Count real guard rereads too; these are generous contractual bounds,
            # not assertions about exact chunk counts or warmed wall-clock time.
            self.assertLess(captured['metrics']['bytes_read'], 18 * MIB)
            self.assertLess(verified['metrics']['bytes_read'], 40 * MIB)
            self.assertGreater(captured['metrics']['bytes_read'], 0)
            self.assertGreater(verified['metrics']['bytes_read'], 0)
            samples.append({'source_bytes': self.path.stat().st_size, 'capture': captured['metrics'], 'verify': verified['metrics']})
        self.assertGreater(samples[1]['source_bytes'], 180 * MIB)
        self.assertLess(samples[1]['capture']['bytes_read'], samples[1]['source_bytes'] // 4)
        self.assertLess(samples[1]['verify']['bytes_read'], samples[1]['source_bytes'] // 4)
        self.io_samples = samples  # Available to the explicit acceptance runner, never production state.


if __name__ == '__main__':
    unittest.main()
