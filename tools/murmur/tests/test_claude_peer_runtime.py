"""Real host/SQL lifecycle around a controlled native peer transport boundary."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from murmur import config, hooks
from murmur.control import local_tick, network_tick
from murmur.coordinator import Coordinator, routing_binding
from murmur.ledger import Ledger
from murmur.native import DeliveryResult, HookEvent, NativeIdentity
from test_coordinator import FakeBackend


ENGINE = "22222222-2222-4222-8222-222222222222"
PROMPT = "33333333-3333-4333-8333-333333333333"
TURN = "claude:" + ENGINE + ":" + PROMPT
NEXT = "claude:" + ENGINE + ":44444444-4444-4444-8444-444444444444"
PROFILE = {"platform": "darwin", "runtime": "desktop", "version": "2.1.308", "sha256": "a" * 64}


class ClaudePeerRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = Ledger(config.ledger_path(self.root))
        self.addCleanup(lambda: self.ledger.close())
        hooks.initialize(self.ledger.db)
        self.identity = NativeIdentity("claude", "local_11111111-1111-4111-8111-111111111111",
            parent_known=True, target={"native_epoch": "owner-generation", "engine_id": ENGINE,
                "platform": "darwin", "runtime": "desktop", "entrypoint": "claude-desktop"},
            evidence={"claude_peer_transcript_path": "/fixture/private.jsonl",
                      "claude_peer_engine_version": "2.1.308", "claude_peer_capture_epoch": "owner-generation"})
        self.actor = self.identity.actor
        self.binding = routing_binding(self.identity)
        self.ledger.enroll(self.actor, asdict(self.identity), "Claude fixture", "self")
        self.ledger.start_turn(self.actor, self.binding, TURN, native_identity=asdict(self.identity))
        self.original = self.ledger.active_turn(self.actor)
        self.backend = FakeBackend()
        self.backend.add("request", causal="peer-origin")
        self.settings = dict(url="http://fixture.invalid", token="fixture", delivery_enabled=True,
                             busy_context_enabled=True)
        config.save(self.settings, self.root)
        hooks.publish_control(self.ledger, True)
        self.prepare_action = None
        self.send_action = None
        self.sent = []
        self.outcome = DeliveryResult("uncertain", "Socket has no admission proof")
        self.proof = {"status": "unknown", "reason": "No canonical peer record yet"}

    def prepare(self, identity, turn):
        self.assertEqual(identity, self.identity)
        self.assertEqual(turn, TURN)
        if self.prepare_action:
            self.prepare_action()
        return dict(schema=1, kind="claude-peer-v1", actor=self.actor, session_id=identity.session_id,
                    engine_id=ENGINE, epoch=identity.epoch, turn_key=turn, prompt_id=PROMPT,
                    prompt_uuid=PROMPT, profile=PROFILE)

    def emission(self, identity, text, token, request):
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"schema": 1, "kind": "claude-peer-v1", "actor": identity.actor,
                "request_sha256": request_hash, "message_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "murmur:" + token)),
                "text": text, "from": "uds:/fixture/reply.sock", "producer_pid": 1234, "frame_sha256": "b" * 64}

    def send(self, identity, message, token, request, emission):
        attempt = self.ledger.attempt(token)
        self.assertIsNotNone(attempt["emitted_at"])
        self.assertEqual(attempt["emission"], emission)
        self.assertEqual(self.ledger.active_turn(self.actor), self.original)
        self.assertEqual(self.ledger.causal_context(self.actor), {"chain_ids": ["peer-origin"]})
        self.sent.append((identity, message, token, request, emission))
        if self.send_action:
            self.send_action()
        return self.outcome

    def tick(self, stopped=None):
        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.supports_peer_delivery", return_value=True), \
                patch("murmur.native.prepare_peer_receipt", side_effect=self.prepare), \
                patch("murmur.native.peer_emission", side_effect=self.emission), \
                patch("murmur.native.deliver_peer", side_effect=self.send), \
                patch("murmur.native.deliver", side_effect=AssertionError("Busy turn must not use idle queue")):
            return network_tick(self.root, self.actor, stopped or threading.Event())

    def confirm(self, actual=TURN):
        attempt = self.ledger.attempt(self.sent[0][2])
        evidence = dict(source="claude-peer-v1", actor=self.actor, token=attempt["token"],
            epoch=self.identity.epoch, expected_turn_key=TURN, actual_turn_key=actual, turn_key=actual,
            message_id=attempt["emission"]["message_id"], receipt_uuid="55555555-5555-4555-8555-555555555555",
            byte_start=100, profile=PROFILE, delivery="queued_command" if actual == TURN else "peer_prompt")
        with patch("murmur.native.verify_peer_receipt", return_value={"status": "accepted", "evidence": evidence}), \
                patch("murmur.native.verify_context_receipt", side_effect=AssertionError("Peer is not a tool receipt")):
            from murmur import native
            return Coordinator(self.ledger, None, native).reconcile_context(self.actor)

    def submit(self, identity=None, token=None, turn=NEXT):
        captured = HookEvent(identity or self.identity, "submit", "UserPromptSubmit", turn,
            prompt=self.sent[0][1], wake_token=token or self.sent[0][2])
        with patch("murmur.native.parse_hook", return_value=captured):
            return hooks.handle(self.ledger, "claude", "UserPromptSubmit", {"prompt": captured.prompt})

    def test_busy_delivery_seals_once_then_native_admission_preserves_original_task(self):
        self.tick()
        attempt = self.ledger.attempt(self.sent[0][2])
        self.assertEqual(attempt["mode"], "context")
        self.assertIsNone(attempt["tool_id"])
        self.assertEqual(attempt["status"], "pending")
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.tick()  # Socket uncertainty cannot create a second send.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.confirm(), [])
        self.assertEqual(self.ledger.active_turn(self.actor), self.original)
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "admitted")
        self.assertIsNotNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.tick()  # An ignored announced backlog stays quiet.
        self.assertEqual(len(self.sent), 1)
        self.ledger.complete(self.actor, TURN, self.binding)
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")

    def test_receipt_after_stop_restart_preserves_concurrent_arrival(self):
        self.tick()
        self.ledger.complete(self.actor, TURN, self.binding)
        self.ledger.close()
        self.ledger = Ledger(config.ledger_path(self.root))
        self.backend.add("later", when=102)
        from murmur import native
        with patch("murmur.native.verify_peer_receipt", return_value=self.proof):
            unknown = Coordinator(self.ledger, None, native).reconcile_context(self.actor)
        self.assertEqual(len(unknown), 1)
        self.assertEqual(len(self.ledger.pending_attempts(self.actor)), 1)
        Coordinator(self.ledger, self.backend).sync(self.actor)
        self.confirm()
        posts = self.ledger.unread_posts(self.actor)
        self.assertIsNotNone(posts[0]["announced_at"])
        self.assertIsNone(posts[1]["announced_at"])
        self.assertIsNone(self.ledger.active_turn(self.actor))

    def test_cold_history_after_restart_preserves_original_and_notifies_later_arrivals(self):
        self.tick()
        self.ledger.complete(self.actor, TURN, self.binding)
        before = self.ledger.attempt(self.sent[0][2])
        self.ledger.close()
        self.ledger = Ledger(config.ledger_path(self.root))
        self.backend.add("later", when=102)
        with patch("murmur.native.warm_peer_history", return_value=False), \
                patch("murmur.native.verify_peer_receipt", return_value={
                    "status": "unknown", "reason": "Native peer history strict index is warming"}), \
                patch("murmur.native.owner_process_state", return_value={"status": "unknown"}), \
                patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.prepare_idle_target", return_value=self.identity), \
                patch("murmur.native.deliver", return_value=DeliveryResult("accepted")) as deliver:
            result = local_tick(self.root, self.actor, None)
            network_tick(self.root, self.actor, threading.Event())
            network_tick(self.root, self.actor, threading.Event())
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(self.ledger.attempt(before["token"]), before)
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertEqual(len(self.sent), 1)
        deliver.assert_called_once()
        later, = [attempt for attempt in self.ledger.attempts(self.actor)
                  if attempt["token"] != before["token"]]
        self.assertEqual(later["post_ids"], ["later"])
        self.assertEqual((later["mode"], later["transport"]), ("idle", "accepted"))
        self.assertEqual(self.ledger.notification_candidates(self.actor), [])
        unread = self.ledger.unread_posts(self.actor)
        self.assertEqual([post["id"] for post in unread], ["request", "later"])
        self.assertTrue(all(post["announced_at"] is None and post["read_at"] is None for post in unread))

    def test_late_peer_submit_records_actual_turn_without_announcing(self):
        self.tick()
        self.ledger.interrupt(self.actor, TURN, self.binding)
        self.assertEqual(self.submit(), {})
        actual = self.ledger.active_turn(self.actor)
        self.assertEqual(actual["turn_key"], NEXT)
        self.assertIsNone(actual["token"])
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])
        self.confirm(NEXT)
        attempt = self.ledger.attempt(self.sent[0][2])
        self.assertEqual((attempt["expected_turn_key"], attempt["turn_key"]), (TURN, NEXT))
        self.ledger.complete(self.actor, NEXT, self.binding)
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")

    def test_late_submit_different_owner_is_blocked(self):
        self.tick()
        self.ledger.complete(self.actor, TURN, self.binding)
        other = replace(self.identity, target={**self.identity.target, "native_epoch": "different-generation"})
        result = self.submit(identity=other)
        self.assertEqual(result["decision"], "block")
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_known_token_with_altered_prompt_is_not_native_peer_admission(self):
        self.tick()
        self.ledger.complete(self.actor, TURN, self.binding)
        token = self.sent[0][2]
        prompt = "You have new messages. [wake:" + token + "]"
        captured = HookEvent(self.identity, "submit", "UserPromptSubmit", NEXT, prompt=prompt, wake_token=token)
        with patch("murmur.native.parse_hook", return_value=captured):
            result = hooks.handle(self.ledger, "claude", "UserPromptSubmit", {"prompt": prompt})
        self.assertEqual(result["decision"], "block")
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertIsNone(self.ledger.unread_posts(self.actor)[0]["announced_at"])

    def test_delivery_gates_and_disable_during_preparation_prevent_claim(self):
        for changes in ({"delivery_enabled": False}, {"busy_context_enabled": False}, {"busy_context_actors": []}):
            with self.subTest(changes=changes):
                config.save({**self.settings, **changes}, self.root)
                self.tick()
                self.assertEqual(self.ledger.attempts(self.actor), [])
        config.save(self.settings, self.root)
        self.prepare_action = lambda: config.save({**self.settings, "delivery_enabled": False}, self.root)
        self.tick()
        self.assertEqual(self.ledger.attempts(self.actor), [])
        self.assertEqual(self.sent, [])

    def test_socket_exception_stays_uncertain_after_completion(self):
        def fail():
            raise OSError("Response lost after possible write")
        self.send_action = fail
        with self.assertRaises(OSError):
            self.tick()
        self.ledger.complete(self.actor, TURN, self.binding)
        attempt = self.ledger.attempt(self.sent[0][2])
        self.assertEqual((attempt["transport"], attempt["status"]), ("uncertain", "pending"))
        self.assertIsNone(self.ledger.create_attempt(self.actor, self.binding))
        self.confirm()
        self.assertEqual(self.ledger.attempt(attempt["token"])["status"], "completed")

    def test_hook_evidence_survives_same_owner_only(self):
        without = replace(self.identity, evidence={})
        for identity in (without, replace(without, target={**without.target, "native_epoch": "new-generation"})):
            captured = HookEvent(identity, "session_start", "SessionStart")
            with patch("murmur.native.parse_hook", return_value=captured):
                self.assertEqual(hooks.handle(self.ledger, "claude", "SessionStart", {}), {})
            if identity.epoch == self.identity.epoch:
                self.assertEqual(self.ledger.actor(self.actor)["identity"]["evidence"], self.identity.evidence)
            # A passive SessionStart cannot change owner or enroll a session.
            self.assertEqual(self.ledger.actor(self.actor)["identity"]["target"], self.identity.target)

    def test_reregister_preserves_hook_facts_for_the_same_owner_only(self):
        ordinary = replace(self.identity, evidence={})
        coordinator = Coordinator(self.ledger, self.backend)
        coordinator.register(ordinary, "Renamed fixture")
        self.assertEqual(self.ledger.actor(self.actor)["identity"]["evidence"], self.identity.evidence)
        replacement = replace(ordinary, target={**ordinary.target, "native_epoch": "new-owner"})
        coordinator.register(replacement, "New owner")
        self.assertEqual(self.ledger.actor(self.actor)["identity"]["evidence"], {})

    def test_prewrite_failure_does_not_hold_messages_as_possibly_delivered(self):
        with patch.object(self, "emission", side_effect=ValueError("Bad prewrite seal")):
            with self.assertRaises(ValueError):
                self.tick()
        attempt = self.ledger.attempts(self.actor)[0]
        self.assertEqual((attempt["transport"], attempt["status"]), ("rejected", "retired"))
        self.assertIsNone(attempt["emitted_at"])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.ledger._unread(self.actor, eligible=True)), 1)

    def test_missing_lease_and_unsupported_profile_keep_busy_session_quiet(self):
        hooks.publish_control(self.ledger, False)
        self.tick()
        self.assertEqual(self.sent, [])
        hooks.publish_control(self.ledger, True)
        with patch("murmur.control.Backend", return_value=self.backend), \
                patch("murmur.native.supports_peer_delivery", return_value=False), \
                patch("murmur.native.prepare_peer_receipt", side_effect=AssertionError("Unsupported profile")), \
                patch("murmur.native.deliver", side_effect=AssertionError("Must not replace busy work")):
            network_tick(self.root, self.actor, threading.Event())
        self.assertEqual(self.ledger.attempts(self.actor), [])


if __name__ == "__main__":
    unittest.main()
