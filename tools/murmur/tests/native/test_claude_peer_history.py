"""Real-file proofs for the ephemeral Desktop reader; no native histories."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import weakref

from murmur.native import claude_peer_history as history
from murmur.native.types import NativeError


BINDING = ('claude:fixture', 'engine-fixture', 'native-epoch', '2.1.309', 'profile-sha256')


def node(number, parent=None, **extra):
    return {'type': 'user', 'uuid': str(uuid.UUID(int=number)), 'parentUuid': parent,
            'isSidechain': False, 'sessionId': 'engine-fixture', 'version': '2.1.309',
            'message': {'role': 'user', 'content': 'private body is never cached'}, **extra}


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode() + b'\n'


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / 'history.jsonl'
        self.root = node(1)
        self.next = node(2, self.root['uuid'], type='assistant')
        self.cache = history.HistoryCache()
        self.write(self.root, self.next)

    def write(self, *rows):
        self.path.write_bytes(b''.join(encoded(row) for row in rows))
        self.path.chmod(0o600)

    def append(self, *rows):
        with self.path.open('ab') as stream:
            stream.write(b''.join(encoded(row) for row in rows))

    def snapshot(self, **kwargs):
        return self.cache.snapshot(self.path, BINDING, time.monotonic() + 2, **kwargs)

    def seal(self):
        with self.snapshot() as view:
            result = view.size, view.sha256
        return result

    def test_strict_lazy_views_have_offsets_main_order_and_no_cached_bodies(self):
        metadata = {'type': 'queue-operation', 'opaque': 'not a conversation'}
        side = node(3, self.next['uuid'], isSidechain=True)
        end = node(4, self.next['uuid'])
        self.write(self.root, metadata, side, self.next, end)
        with self.snapshot() as view:
            self.assertEqual(view.node(end['uuid']), end)
            self.assertEqual(view.position(self.next['uuid']), len(encoded(self.root) + encoded(metadata) + encoded(side)))
            self.assertEqual(view.main_position(end['uuid']), 2)
            self.assertEqual(view.previous_main(end['uuid']), self.next)
            self.assertEqual(view.last_main(), end)
            self.assertEqual(list(view.main_ids_before(end['uuid'])), [self.root['uuid'], self.next['uuid']])
            self.assertEqual([row for _, row in view.records()], [self.root, metadata, side, self.next, end])
            entry = view._entry
            self.assertTrue(all(key is None or isinstance(key, bytes) and len(key) == 16 for key in entry.keys))
            self.assertFalse(any(isinstance(value, dict) for value in entry.__dict__.values()
                                 if value is not entry.lookup))
        with self.assertRaisesRegex(NativeError, 'outside its snapshot'):
            view.node(end['uuid'])

    def test_frozen_view_does_not_retroactively_find_appended_parent_or_main_head(self):
        missing = node(3)
        first = node(1, missing['uuid'])
        self.write(first)
        seal = self.seal()
        self.append(missing, self.next)
        with self.snapshot(prefix=seal) as current:
            frozen = current.before(seal[0])
            self.assertEqual(current.node(missing['uuid']), missing)
            self.assertIsNone(frozen.node(missing['uuid']))
            self.assertIsNone(frozen.position(missing['uuid']))
            self.assertIsNone(frozen.main_position(missing['uuid']))
            self.assertIsNone(frozen.previous_main(missing['uuid']))
            self.assertEqual(frozen.last_main(), first)
            self.assertEqual(list(frozen.records()), [(0, first)])
            self.assertEqual(frozen.sha256, seal[1])
            with self.assertRaises(NativeError):
                frozen.before(current.size)
            with self.assertRaises(NativeError):
                frozen.last_main(current.size)
            with self.assertRaises(NativeError):
                frozen.before(seal[0] - 1)
            with self.assertRaises(NativeError):
                list(current.records(stop=seal[0] - 1))
            with self.assertRaises(NativeError):
                list(current.records(start=1))
            with self.assertRaises(NativeError):
                current.last_main(seal[0] - 1)

    def test_append_requires_fresh_full_cache_frontier_not_just_short_attempt_prefix(self):
        self.write(self.root)
        short_seal = self.seal()
        later = node(3, self.root['uuid'])
        self.append(self.next, later)
        self.seal()  # A larger published cache exists than the attempt's seal.
        data = self.path.read_bytes().replace(b'private body', b'changed body', 3)
        # Leave the short seal unchanged, while overwriting a cached later row.
        original_root = encoded(self.root)
        data = original_root + data[len(original_root):]
        self.path.write_bytes(data + encoded(node(4, later['uuid'])))
        with self.assertRaisesRegex(NativeError, 'indexed prefix was overwritten'):
            with self.snapshot(prefix=short_seal):
                self.fail('stale index became an admission view')
        self.assertFalse(next(iter(self.cache._entries.values())).published)

    def test_attempt_prefix_rewrite_is_rejected_after_restart_and_eviction(self):
        seal = self.seal()
        self.cache.evict()
        self.path.write_bytes(self.path.read_bytes().replace(b'private body', b'changed body'))
        self.append(node(3))
        for cache in (self.cache, history.HistoryCache()):
            with self.subTest(cache=cache):
                with self.assertRaisesRegex(NativeError, 'captured prefix was overwritten'):
                    with cache.snapshot(self.path, BINDING, time.monotonic() + 2, prefix=seal):
                        self.fail('restart lost the immutable attempt seal')

    def test_rotation_builds_new_generation_and_exposes_frozen_identity_check(self):
        with self.snapshot() as view:
            original, seal = (view.facts.st_dev, view.facts.st_ino), (view.size, view.sha256)
        replacement = self.path.with_suffix('.new')
        replacement.write_bytes(self.path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, self.path)
        with self.snapshot(prefix=seal) as view:
            # The caller also compares these facts to its frozen request.
            self.assertNotEqual((view.facts.st_dev, view.facts.st_ino), original)
            self.assertEqual(len(self.cache._entries), 2)

    def test_truncation_and_partial_records_never_publish(self):
        seal = self.seal()
        self.path.write_bytes(encoded(self.root))
        with self.assertRaisesRegex(NativeError, 'invalid or truncated'):
            with self.snapshot(prefix=seal):
                self.fail('truncation was accepted')
        self.cache.evict()
        with self.path.open('ab') as stream:
            stream.write(b'{"unfinished":')
        self.assertFalse(self.cache.warm(self.path, BINDING, time.monotonic() + 2))
        self.assertFalse(next(iter(self.cache._entries.values())).published)
        with self.path.open('ab') as stream:
            stream.write(b'true}\n')
        self.assertTrue(self.cache.warm(self.path, BINDING, time.monotonic() + 2))

    def test_global_uuid_depth_line_and_json_validation_are_not_tail_only(self):
        deep = {'type': 'metadata'}
        cursor = deep
        for _ in range(130):
            cursor['child'] = {}
            cursor = cursor['child']
        cases = [encoded(self.root) + encoded(self.root), encoded(deep),
                 b'{"type":"metadata","value":"' + b'x' * history._MAX_LINE + b'"}\n',
                 b'{"type":"metadata","x":1,"x":2}\n',
                 b'{"type":"metadata","x":NaN}\n', b'[]\n', b'{bad}\n']
        for data in cases:
            with self.subTest(prefix=data[:50]):
                self.cache.evict()
                self.path.write_bytes(data)
                with self.assertRaises(NativeError):
                    with self.snapshot():
                        self.fail('invalid historical row was accepted')

    def test_provisional_parse_resumes_and_recertifies_all_parsed_bytes_before_publish(self):
        self.write(*(node(i + 1) for i in range(200)))
        original = self.path.read_bytes()
        seal = len(original), hashlib.sha256(original).hexdigest()
        base = time.monotonic()
        ticks = iter(base + i / 10000 for i in range(10000))
        with patch.object(history.time, 'monotonic', side_effect=lambda: next(ticks)):
            self.assertFalse(self.cache.warm(self.path, BINDING, base + .004))
        entry = next(iter(self.cache._entries.values()))
        self.assertGreater(entry.frontier, 0)
        self.assertLess(entry.frontier, self.path.stat().st_size)
        self.assertFalse(entry.published)
        self.path.write_bytes(self.path.read_bytes().replace(b'private body', b'changed body', 1))
        self.append(node(201))
        with self.assertRaisesRegex(NativeError, 'indexed prefix was overwritten'):
            with self.snapshot(prefix=seal):
                self.fail('mixed provisional generations were published')
        self.assertTrue(self.cache.warm(self.path, BINDING, time.monotonic() + 2))

    def test_fresh_capture_rebuilds_rewritten_cache_before_parsing_stale_offsets(self):
        old_seal = self.seal()
        replacement = node(3, extra_metadata='different row length ' * 20)
        end = node(4, replacement['uuid'])
        self.write(replacement, end)
        with self.snapshot() as view:
            self.assertEqual(view.node(replacement['uuid']), replacement)
            self.assertIsNone(view.node(self.root['uuid']))
            self.assertEqual(view.last_main(), end)
        with self.assertRaisesRegex(NativeError, 'captured prefix was overwritten'):
            with self.snapshot(prefix=old_seal):
                self.fail('fresh cache rebuild changed the frozen attempt seal')

    def test_mutation_after_hash_during_lazy_lookup_aborts_context(self):
        seal = self.seal()
        with self.assertRaisesRegex(NativeError, 'changed during its snapshot'):
            with self.snapshot(prefix=seal) as view:
                self.assertEqual(view.node(self.root['uuid']), self.root)
                self.path.write_bytes(self.path.read_bytes().replace(b'private body', b'changed body', 1))
                view.node(self.next['uuid'])
        with self.assertRaises(NativeError):
            view.node(self.root['uuid'])

    def test_path_replacement_during_lease_aborts_even_with_original_read_fd(self):
        with self.assertRaisesRegex(NativeError, 'changed during its snapshot'):
            with self.snapshot() as view:
                replacement = self.path.with_suffix('.new')
                replacement.write_bytes(self.path.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, self.path)
                self.assertEqual(view.node(self.root['uuid']), self.root)

    def test_mode_changes_symlinks_and_resource_bounds_fail_visibly(self):
        with self.assertRaisesRegex(NativeError, 'ownership, mode'):
            with self.snapshot():
                self.path.chmod(0o644)
        self.path.chmod(0o600)
        actual = self.path.with_suffix('.actual')
        self.path.rename(actual)
        self.path.symlink_to(actual)
        with self.assertRaisesRegex(NativeError, 'canonical'):
            with self.snapshot():
                self.fail('symlink was followed')
        self.path.unlink()
        actual.rename(self.path)
        for cache in (history.HistoryCache(max_records=1), history.HistoryCache(max_uuids=1),
                      history.HistoryCache(max_bytes=history._SCRATCH_BYTES)):
            with self.assertRaisesRegex(NativeError, 'resource bounds|memory budget'):
                with cache.snapshot(self.path, BINDING, time.monotonic() + 2):
                    self.fail('resource bound was silently exceeded')
            self.assertLessEqual(cache.accounted_bytes, cache.max_bytes)

    def test_concurrent_readers_share_builder_and_lease_prevents_eviction(self):
        entered, release = threading.Event(), threading.Event()
        def first():
            with self.snapshot() as view:
                entered.set()
                release.wait(2)
                return view.node(self.root['uuid'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(first)
            self.assertTrue(entered.wait(2))
            self.cache.evict()
            self.assertEqual(len(self.cache._entries), 1)
            def second():
                with self.cache.snapshot(self.path, BINDING, time.monotonic() + .03):
                    self.fail('same generation acquired a second lease')
            waiting = pool.submit(second)
            with self.assertRaises(history.HistoryPending):
                waiting.result(2)
            release.set()
            self.assertEqual(future.result(2), self.root)
        self.assertEqual(len(self.cache._entries), 1)
        self.assertTrue(self.cache.warm(self.path, BINDING, time.monotonic() + 2))
        self.cache.evict()
        self.assertEqual(self.cache.accounted_bytes, 0)

    def test_budget_evicts_only_unleased_generations_and_binding_is_not_reused(self):
        self.cache = history.HistoryCache(max_bytes=history._SCRATCH_BYTES + 6_000)
        clock = [100.0]
        self.cache._clock = lambda: clock[0]
        self.seal()
        changed = (*BINDING[:-1], 'other-executable-profile')
        with self.assertRaises(history.HistoryPending):
            with self.cache.snapshot(self.path, changed, time.monotonic() + 2):
                self.fail('fixed publication grace was ignored')
        clock[0] += history._PUBLICATION_GRACE + 1
        with self.cache.snapshot(self.path, changed, time.monotonic() + 2) as view:
            self.assertEqual(view.node(self.root['uuid']), self.root)
            self.assertEqual(len(self.cache._entries), 1)
            self.assertEqual(view._entry.key[0], changed)
        self.assertLessEqual(self.cache.accounted_bytes, self.cache.max_bytes)

    def test_concurrent_cold_readers_do_not_create_two_builders(self):
        entered, release = threading.Event(), threading.Event()
        validator = history._records
        calls = []
        def blocked_record(data, deadline):
            calls.append(data)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            return validator(data, deadline)
        with patch.object(history, '_records', side_effect=blocked_record):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.cache.warm, self.path, BINDING, time.monotonic() + 2)
                self.assertTrue(entered.wait(2))
                second = pool.submit(self.cache.warm, self.path, BINDING, time.monotonic() + .03)
                self.assertFalse(second.result(2))
                self.assertEqual(len(self.cache._entries), 1)
                self.assertEqual(len(calls), 1)
                release.set()
                self.assertTrue(first.result(2))
        self.assertEqual(len(calls), 2)

    def test_pinned_index_and_scratch_are_charged_before_another_builder(self):
        self.cache = history.HistoryCache(max_bytes=history._SCRATCH_BYTES + 6_000)
        other = self.path.with_name('other.jsonl')
        other.write_bytes(encoded(self.root))
        other.chmod(0o600)
        with self.snapshot() as view:
            with self.assertRaisesRegex(NativeError, 'memory budget'):
                with self.cache.snapshot(other, BINDING, time.monotonic() + 1):
                    self.fail('a new builder evicted a live index or exceeded its budget')
            self.assertEqual(view.node(self.root['uuid']), self.root)
            self.assertEqual(len(self.cache._entries), 1)
            self.assertLessEqual(self.cache.accounted_bytes, self.cache.max_bytes)

    def test_cold_builder_releases_each_tree_before_decoding_the_next_record(self):
        class TrackedRecord(dict):
            pass
        validator, previous = history._records, []
        def tracked(data, deadline):
            if previous:
                self.assertIsNone(previous[-1](), 'previous decoded body overlaps the next JSON parse')
            rows, lookup = validator(data, deadline)
            value = TrackedRecord(rows[0][1])
            previous.append(weakref.ref(value))
            return [(0, value)], {key: value for key in lookup}
        with patch.object(history, '_records', side_effect=tracked):
            self.assertTrue(self.cache.warm(self.path, BINDING, time.monotonic() + 2))
        self.assertEqual(len(previous), 2)
        self.assertTrue(all(ref() is None for ref in previous))

    def test_constrained_round_robin_large_indexes_eventually_progress_without_hot_read_renewal(self):
        count, paths = 5_000, []
        for number in range(3):
            path = self.path.with_name(f'actor-{number}.jsonl')
            path.write_bytes(b''.join(encoded(node(i + 1)) for i in range(count)))
            path.chmod(0o600)
            paths.append(path)
        # Enough for one complete large index, queued headers and one lease;
        # not enough for two full indexes. Old LRU-only admission churns here.
        one = history._ENTRY_BYTES + count * (history._ROW_BYTES + history._UUID_BYTES)
        self.cache = history.HistoryCache(max_bytes=history._SCRATCH_BYTES + one + 16_384)
        clock, completed, first_grace = [100.0], set(), None
        self.cache._clock = lambda: clock[0]
        for _ in range(400):
            for number, path in enumerate(paths):
                if self.cache.warm(path, (*BINDING, str(number)), time.monotonic() + .005):
                    completed.add(number)
                    if number == 0:
                        entry = next(e for e in self.cache._entries.values() if e.key[1] == str(path))
                        if first_grace is None:
                            first_grace = entry.retain_until
                        elif clock[0] < first_grace:
                            self.assertEqual(entry.retain_until, first_grace)
                self.assertLessEqual(self.cache.accounted_bytes, self.cache.max_bytes)
            if len(completed) == 3:
                break
            clock[0] += .5
        self.assertEqual(completed, {0, 1, 2}, 'round-robin actors repeatedly evicted partial builders')

    def test_abandoned_builder_and_hard_bound_release_fifo_admission(self):
        clock = [100.0]
        self.cache._clock = lambda: clock[0]
        self.path.write_bytes(encoded(self.root) + b'{"unfinished":')
        other = self.path.with_name('other.jsonl')
        other.write_bytes(encoded(self.root))
        other.chmod(0o600)
        self.assertFalse(self.cache.warm(self.path, BINDING, time.monotonic() + 1))
        self.assertFalse(self.cache.warm(other, BINDING, time.monotonic() + 1))
        clock[0] += history._BUILDER_STALE - 1
        self.assertFalse(self.cache.warm(self.path, BINDING, time.monotonic() + 1))
        clock[0] += 2  # Failed reads did not refresh progress protection.
        self.assertTrue(self.cache.warm(other, BINDING, time.monotonic() + 1))
        self.assertIsNone(self.cache._builder)
        limited = history.HistoryCache(max_records=1)
        self.write(self.root, self.next)
        with self.assertRaisesRegex(NativeError, 'resource bounds'):
            limited.warm(self.path, BINDING, time.monotonic() + 1)
        self.assertIsNone(limited._builder)
        self.assertTrue(limited.warm(other, BINDING, time.monotonic() + 1))
        self.cache.evict()
        self.path.write_bytes(encoded(self.root) + b'{"unfinished":')
        self.assertFalse(self.cache.warm(self.path, BINDING, time.monotonic() + 1))
        self.path.write_bytes(b'{}\n')
        self.assertTrue(self.cache.warm(other, BINDING, time.monotonic() + 1))

    def test_hard_parse_error_and_gone_builder_do_not_starve_another_file(self):
        other = self.path.with_name('other.jsonl')
        other.write_bytes(encoded(self.root))
        other.chmod(0o600)
        self.path.write_bytes(b'{bad}\n')
        with self.assertRaises(NativeError):
            self.cache.warm(self.path, BINDING, time.monotonic() + 1)
        self.assertIsNone(self.cache._builder)
        self.assertTrue(self.cache.warm(other, BINDING, time.monotonic() + 1))
        self.cache.evict()
        self.path.write_bytes(encoded(self.root) + b'{"unfinished":')
        self.assertFalse(self.cache.warm(self.path, BINDING, time.monotonic() + 1))
        self.path.unlink()
        self.assertTrue(self.cache.warm(other, BINDING, time.monotonic() + 1))

    def test_invalid_attempt_seal_releases_a_valid_current_index_without_stalling_others(self):
        old_seal = len(self.path.read_bytes()), hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.write(node(3, extra_metadata='new valid history ' * 20))
        other = self.path.with_name('other.jsonl')
        other.write_bytes(encoded(self.root))
        other.chmod(0o600)
        self.cache = history.HistoryCache(max_bytes=history._SCRATCH_BYTES + 6_000)
        with self.assertRaisesRegex(NativeError, 'captured prefix was overwritten'):
            with self.snapshot(prefix=old_seal):
                self.fail('invalid attempt was admitted')
        entry = next(iter(self.cache._entries.values()))
        self.assertTrue(entry.published)
        self.assertIsNone(self.cache._builder)
        self.assertFalse(self.cache._waiting)
        self.assertEqual(entry.retain_until, 0)
        self.assertTrue(self.cache.warm(other, BINDING, time.monotonic() + 1))

    @unittest.skipUnless(os.environ.get('MURMUR_LARGE_HISTORY_TEST') == '1', 'opt-in 718 MiB real-file pipeline')
    def test_718_mib_strict_json_cold_warm_pipeline(self):
        count, conversations, target = 325_850, 190_291, 754_293_312
        size = 0
        base, extra = divmod(target, count)
        with self.path.open('wb') as stream:
            for number in range(count):
                row = node(number + 1) if number < conversations else {'type': 'queue-operation'}
                # Real JSON strings and unique conversation UUIDs, not sparse holes.
                row['padding'] = ''
                row['padding'] = 'x' * (base + (number < extra) - len(encoded(row)))
                line = encoded(row)
                stream.write(line)
                size += len(line)
        self.assertEqual(size, target)
        self.assertLess(len(line), history._MAX_LINE)
        started, steps = time.monotonic(), 0
        while not self.cache.warm(self.path, BINDING, time.monotonic() + .5):
            steps += 1
            self.assertLess(time.monotonic() - started, 90, 'bounded synthetic cold build did not progress')
        cold_ms = (time.monotonic() - started) * 1000
        with self.snapshot() as view:
            seal = view.size, view.sha256
            self.assertEqual(len(view._entry.lookup), conversations)
            self.assertEqual(len(view._entry.starts), count)
        warm_started = time.monotonic()
        with self.cache.snapshot(self.path, BINDING, warm_started + .5, prefix=seal) as view:
            self.assertEqual(view.node(str(uuid.UUID(int=conversations)))['uuid'], str(uuid.UUID(int=conversations)))
            self.assertEqual(view.last_main()['uuid'], str(uuid.UUID(int=conversations)))
        warm_ms = (time.monotonic() - warm_started) * 1000
        self.assertLess(warm_ms, 500)
        print(json.dumps({'synthetic_bytes': size, 'records': count, 'uuids': conversations,
                          'cold_ms': round(cold_ms, 2), 'warm_ms': round(warm_ms, 2), 'warm_steps': steps,
                          'accounted_retained_bytes': self.cache.accounted_bytes}))


if __name__ == '__main__':
    unittest.main()
