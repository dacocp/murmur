"""The live control lane makes progress while isolated network work is blocked."""

from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from murmur import config, daemon, hooks
from murmur.backend import BackendError
from murmur.control import HostControl, network_tick
from murmur.coordinator import routing_binding
from murmur.ledger import Ledger
from murmur.native import DeliveryResult, HookEvent, NativeIdentity, parse_wake
from test_coordinator import FakeBackend


PARENT = NativeIdentity("codex", "11111111-1111-4111-8111-111111111111", parent_known=True)
OTHER = replace(PARENT, session_id="33333333-3333-4333-8333-333333333333")


class HostControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = config.ledger_path(self.root)
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)
        hooks.initialize(self.ledger.db)
        for identity in (PARENT, OTHER):
            self.ledger.enroll(identity.key, asdict(identity), "Fixture", "self")
            self.ledger.sync_conversations(identity.key, [dict(id="build", name="Build", kind="channel", role="member")])
        config.save(dict(url="http://fixture.invalid", token="fixture", delivery_enabled=True,
                         busy_context_enabled=True), self.root)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail("Expected independent scheduler progress did not occur")

    def start(self, *, clock=time.monotonic):
        stopped = threading.Event()
        errors = []

        def run():
            ledger = Ledger(self.path)
            try:
                HostControl(self.root, ledger, stopped, interval=0.005, workers=2, clock=clock).run()
            except BaseException as error:
                errors.append(error)
            finally:
                ledger.close()

        thread = threading.Thread(target=run)
        thread.start()

        def stop():
            stopped.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.addCleanup(stop)
        return stopped, thread

    def native_hook(self, identity, event, turn, *, message="", token=None):
        captured = HookEvent(identity, "submit" if event == "UserPromptSubmit" else "complete",
                             event, turn, prompt=message, wake_token=token)
        ledger = Ledger(self.path)
        try:
            with patch("murmur.native.parse_hook", return_value=captured):
                self.assertEqual(hooks.handle(ledger, "codex", event, {"prompt": message}), {})
        finally:
            ledger.close()

    def test_pending_handoffs_survive_restart_without_resending_their_cohort(self):
        identities = {identity.key: replace(identity, evidence={"desktop_wake": {
            "fixture_owner": "captured-desktop-generation"}}) for identity in (PARENT, OTHER)}
        outcomes = {PARENT.key: "accepted", OTHER.key: "uncertain"}
        for actor, identity in identities.items():
            self.ledger.update_identity(actor, asdict(identity))
        backend = FakeBackend()
        backend.add("request")
        handoffs = []

        def handoff(identity, message):
            # The native boundary may enqueue and wake before its response is lost.
            # Its durable recipient/token claim must already exist at that point.
            ledger = Ledger(self.path)
            try:
                attempt = ledger.attempt(parse_wake(message))
                self.assertEqual(attempt["token"], parse_wake(message))
                self.assertEqual(attempt["native_identity"], asdict(identity))
                self.assertIn(attempt["post_ids"], (["request"], ["later-request"]))
                self.assertIsNone(attempt["admitted_at"])
            finally:
                ledger.close()
            handoffs.append((identity.key, parse_wake(message)))
            return DeliveryResult(outcomes[identity.key], "Queue committed; wake response may be lost")

        with patch("murmur.control.Backend", return_value=backend), \
                patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity), \
                patch("murmur.native.deliver", side_effect=handoff) as deliver, \
                patch("murmur.native.owner_process_state", return_value={"status": "unknown"}):
            daemon.run(self.root, once=True)
            self.assertEqual(len(handoffs), 2)
            before = {actor: self.ledger.attempt(token) for actor, token in handoffs}
            self.ledger.close()
            self.ledger = Ledger(self.path)
            self.addCleanup(self.ledger.close)
            backend.add("later-request", when=102)
            # The unresolved original stays frozen, while a new coordinator
            # can notify the distinct arrival without replaying the first batch.
            daemon.run(self.root, once=True)
            daemon.run(self.root, once=True)
        self.assertEqual(deliver.call_count, 4)
        self.assertEqual(len(handoffs), 4)
        for actor, original in before.items():
            attempts = self.ledger.attempts(actor)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(self.ledger.attempt(original["token"]), original)
            later, = [attempt for attempt in attempts if attempt["token"] != original["token"]]
            self.assertEqual(later["post_ids"], ["later-request"])
            self.assertEqual(later["transport"], outcomes[actor])
            self.assertEqual(self.ledger.notification_candidates(actor), [])
            self.assertEqual(original["status"], "pending")
            self.assertEqual(original["transport"], outcomes[actor])
            unread = self.ledger.unread_posts(actor)
            self.assertEqual([post["id"] for post in unread], ["request", "later-request"])
            self.assertTrue(all(post["announced_at"] is None for post in unread))

    def test_native_admission_and_completion_can_precede_transport_response(self):
        backend = FakeBackend()
        backend.add("request")
        turns = {PARENT.key: "codex:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                 OTHER.key: "codex:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}

        def handoff(identity, message):
            token = parse_wake(message)
            self.native_hook(identity, "UserPromptSubmit", turns[identity.key], message=message, token=token)
            if identity.key == OTHER.key:
                self.native_hook(identity, "Stop", turns[identity.key])
            return DeliveryResult("uncertain", "Wake was processed before its IPC response was lost")

        with patch("murmur.control.Backend", return_value=backend), \
                patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity), \
                patch("murmur.native.deliver", side_effect=handoff) as deliver:
            for identity in (PARENT, OTHER):
                network_tick(self.root, identity.key, threading.Event())
                network_tick(self.root, identity.key, threading.Event())
        self.assertEqual(deliver.call_count, 2)
        for actor, expected in ((PARENT.key, "admitted"), (OTHER.key, "completed")):
            attempt, = self.ledger.attempts(actor)
            self.assertEqual(attempt["status"], expected)
            self.assertEqual(attempt["transport"], "uncertain")
            self.assertEqual(attempt["turn_key"], turns[actor])
            self.assertIsNotNone(attempt["admitted_at"])
            self.assertIsNotNone(self.ledger.unread_posts(actor)[0]["announced_at"])
        self.assertEqual(self.ledger.active_turn(PARENT.key)["turn_key"], turns[PARENT.key])
        self.assertIsNone(self.ledger.active_turn(OTHER.key))

    def test_ordinary_submit_during_idle_preflight_preserves_work_without_handoff(self):
        backend = FakeBackend()
        backend.add("request")
        turn = "codex:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

        def preflight(identity):
            self.native_hook(identity, "UserPromptSubmit", turn, message="Continue the original task")
            return identity

        with patch("murmur.control.Backend", return_value=backend), \
                patch("murmur.native.prepare_idle_target", side_effect=preflight), \
                patch("murmur.native.deliver", return_value=DeliveryResult("accepted")) as deliver:
            network_tick(self.root, PARENT.key, threading.Event())
        deliver.assert_not_called()
        self.assertEqual(self.ledger.active_turn(PARENT.key)["turn_key"], turn)
        self.assertEqual(self.ledger.attempts(PARENT.key), [])
        self.assertIsNone(self.ledger.unread_posts(PARENT.key)[0]["announced_at"])
        self.native_hook(PARENT, "Stop", turn)
        with patch("murmur.control.Backend", return_value=backend), \
                patch("murmur.native.prepare_idle_target", side_effect=lambda identity: identity), \
                patch("murmur.native.deliver", return_value=DeliveryResult("accepted")) as deliver:
            network_tick(self.root, PARENT.key, threading.Event())
        deliver.assert_called_once()
        attempt, = self.ledger.attempts(PARENT.key)
        self.assertEqual(attempt["status"], "pending")
        self.assertEqual(attempt["post_ids"], ["request"])

    def test_slow_network_does_not_stop_receipts_lease_or_other_actor(self):
        self.ledger.ingest(PARENT.key, [dict(id="request", channel_id="build", user_id="peer",
            root_id="", create_at=1, update_at=1)], 1, 1)
        epoch = routing_binding(PARENT)
        self.ledger.start_turn(PARENT.key, epoch, "turn", native_identity=asdict(PARENT))
        self.ledger.claim_context(PARENT.key, epoch, "turn", "tool", native_identity=asdict(PARENT), token="context")
        slow_started, release = threading.Event(), threading.Event()
        other_progress = []

        class SlowBackend(FakeBackend):
            def changes(inner, actor, cursor, generation):
                if actor == PARENT.key:
                    slow_started.set()
                    if not release.wait(3):
                        raise BackendError("Fixture release timed out")
                else:
                    other_progress.append(actor)
                # An empty generation reset is valid while preserving metadata.
                page = super().changes(actor, 0, generation)
                page["cursor"] = cursor
                return page

        with patch("murmur.control.Backend", return_value=SlowBackend()), \
                patch("murmur.native.verify_context_receipt", return_value={"status": "accepted", "evidence": {"exact": True}}), \
                patch("murmur.native.owner_process_state", return_value={"status": "unknown"}):
            stopped, thread = self.start()
            try:
                self.assertTrue(slow_started.wait(2))
                self.wait_for(lambda: self.ledger.attempt("context")["status"] == "admitted")
                self.wait_for(lambda: hooks.busy_context_enabled(self.ledger, PARENT.key))
                self.wait_for(lambda: len(other_progress) >= 2)
                self.assertFalse(release.is_set())
                self.assertEqual(self.ledger.active_turn(PARENT.key)["turn_key"], "turn")
            finally:
                stopped.set()
                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_stalled_local_owner_loses_only_its_lease(self):
        now = [0.0]
        release, stalled = threading.Event(), threading.Event()
        calls = []

        def maintenance(root, actor, after_token):
            if actor == PARENT.key:
                calls.append(actor)
                if len(calls) > 1:
                    stalled.set()
                    release.wait(3)
            return {"token": None, "unknown": [], "recovery": None}

        with patch("murmur.control.local_tick", side_effect=maintenance), \
                patch("murmur.control.Backend", return_value=FakeBackend()):
            stopped, thread = self.start(clock=lambda: now[0])
            try:
                self.wait_for(lambda: hooks.busy_context_enabled(self.ledger, PARENT.key))
                self.assertTrue(stalled.wait(2))
                now[0] = 11.0
                self.wait_for(lambda: not hooks.busy_context_enabled(self.ledger, PARENT.key))
                self.wait_for(lambda: hooks.busy_context_enabled(self.ledger, OTHER.key))
                self.assertFalse(release.is_set())
            finally:
                stopped.set()
                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_unknown_old_operation_does_not_starve_later_recovery(self):
        settings = config.load(self.root)
        settings["delivery_enabled"] = False
        config.save(settings, self.root)
        for op_id in ("a", "b"):
            self.ledger.prepare_outbound(PARENT.key, dict(action="post", channel_id="build", message=op_id), op_id)
        backend = FakeBackend()
        backend.operations["b"] = dict(status="complete", result=dict(post_id="post-b", channel_id="build"))
        with patch("murmur.control.Backend", return_value=backend):
            first = network_tick(self.root, PARENT.key, threading.Event())
            network_tick(self.root, PARENT.key, threading.Event(), first["op_id"])
        self.assertEqual(backend.operation_calls, ["a", "b"])
        self.assertEqual(self.ledger.outbound("a")["state"], "uncertain")
        self.assertEqual(self.ledger.outbound("b")["state"], "complete")


if __name__ == "__main__":
    unittest.main()
