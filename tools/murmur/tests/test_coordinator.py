"""Exercise runtime coordination with canonical content behind a fake HTTP boundary."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murmur import __main__ as cli
from murmur import hooks
from murmur.backend import BackendError
from murmur.coordinator import Coordinator, actor_key
from murmur.ledger import Ledger, LedgerError
from murmur.native import DeliveryResult, HookEvent, NativeIdentity, parse_wake


SESSION = "11111111-1111-4111-8111-111111111111"
TURN = "codex:22222222-2222-4222-8222-222222222222"


class FakeBackend:
    """Server-only content and operation receipts, with explicit failure controls."""
    def __init__(self):
        self.chats = [{"id": "build", "name": "Build review", "kind": "channel", "role": "member"}]
        self.canonical = {}
        self.events = []
        self.operations = {}
        self.register_calls = []
        self.register_prefixes = []
        self.mutation_calls = []
        self.operation_calls = []
        self.lose_response = False
        self.commit_before_failure = True
        self.before_mutation = None

    def request(self, method, path):
        if (method, path) == ("GET", "/health"):
            return {"capabilities": {"attention_boundaries": {"version": 1}}}
        raise AssertionError("Unexpected fixture request")

    def attention_boundary(self, actor, channel):
        chat = next((c for c in self.conversations(actor) if c["id"] == channel), None)
        if chat is None or chat["role"] != "member":
            raise BackendError("Full membership required", status=403)
        return {"conversation_id": channel, "after_seq": len(self.events)}

    def register(self, actor, name, parent, *, username_prefix=None, enrollment_id=None, lineage=None, mode=None):
        self.register_calls.append((actor, name, parent))
        self.register_prefixes.append(username_prefix)
        return {"id": actor, "name": name, "user_id": "self", "enrollment_seq": 0}

    def conversations(self, actor):
        return [dict(c) for c in self.chats]

    def saved(self, actor):
        return []

    def descriptor(self, actor, channel):
        chat = next(c for c in self.chats if c["id"] == channel)
        return dict(chat, open=chat["kind"] == "channel", purpose="",
                    visibility="public" if chat["kind"] == "channel" else "private")

    def add(self, post_id, *, channel="build", when=101, root="", user="peer", causal=None):
        post = {"id": post_id, "channel_id": channel, "user_id": user, "author": "Bea",
                "message": "Question about " + post_id, "root_id": root,
                "create_at": when, "update_at": when}
        if causal:
            post["causal"] = causal
        self.canonical[post_id] = post
        self.events.append(dict(post))
        return post

    def changes(self, actor, cursor, generation):
        end = min(cursor + 100, len(self.events))
        return {"attention_version": 3, "conversations": self.conversations(actor),
                "changes": [{"seq": i + 1, "first_seen_seq": next(j + 1 for j, old in enumerate(self.events) if old["id"] == p["id"]), "post": dict(p), "notification_eligible": True}
                            for i, p in enumerate(self.events) if cursor <= i < end],
                "cursor": end, "generation": 1, "more": end < len(self.events), "reset": generation != 1}

    def post(self, actor, post_id):
        if post_id not in self.canonical:
            raise BackendError("Post is unavailable", status=404)
        return dict(self.canonical[post_id])

    def root(self, actor, post_id):
        post = self.post(actor, post_id)
        if post["root_id"] or post.get("type", "").startswith("system_"):
            raise BackendError("Requested post is not a discussion root", status=400)
        return post

    def thread(self, actor, root):
        return [dict(p) for p in self.canonical.values() if p["id"] == root or p["root_id"] == root]

    def thread_page(self, actor, root, limit=20, before=None):
        anchor = self.post(actor, root)
        if anchor["root_id"]:
            raise BackendError("Requested post is a reply, not a thread root", status=400)
        posts = self.thread(actor, root)
        if before:
            index = next((i for i, post in enumerate(posts) if post["id"] == before), None)
            if index is None:
                raise BackendError("before post does not belong to this thread", status=400)
            posts = posts[:index]
        page = posts[-limit:]
        return {"root": anchor, "posts": page, "next_before": page[0]["id"] if len(posts) > limit else None}

    def agent_page(self, query="", limit=20, after=None):
        agents = {id: {"id": id, "name": name} for id, name, _ in self.register_calls}
        agents = [a for a in agents.values() if query.casefold() in (a["id"] + " " + a["name"]).casefold()]
        agents.sort(key=lambda a: a["id"])
        if after:
            agents = agents[next(i for i, a in enumerate(agents) if a["id"] == after) + 1:]
        return {"agents": agents[:limit], "next_after": agents[limit-1]["id"] if len(agents) > limit else None}

    def posts(self, actor, channel, limit, before):
        posts = [dict(p) for p in self.canonical.values() if p["channel_id"] == channel]
        if before:
            posts = posts[:next(i for i, p in enumerate(posts) if p["id"] == before)]
        return {"posts": posts[-limit:], "next_before": None}

    def mutate(self, action, actor, op_id, **payload):
        self.mutation_calls.append((action, actor, op_id, payload))
        if self.before_mutation:
            self.before_mutation(op_id)
        result = {"post": {"id": "reply-" + op_id, "channel_id": payload["channel_id"],
                           "message": payload["message"], "root_id": payload.get("root_id", ""),
                           "user_id": "self"}}
        if self.commit_before_failure:
            self.operations[op_id] = {"status": "complete", "result": result}
        if self.lose_response:
            raise BackendError("Response lost after request write", uncertain=True)
        return result

    def operation(self, actor, op_id):
        self.operation_calls.append(op_id)
        if op_id not in self.operations:
            raise BackendError("Operation not confirmed", status=404)
        return self.operations[op_id]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def deliver(self, identity, message):
        self.calls.append((identity, message))
        return DeliveryResult("accepted", "Queue accepted", {"queue_id": "q1"})


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.path = Path(self.home.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.backend = FakeBackend()
        self.transport = FakeTransport()
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.identity = NativeIdentity("codex", SESSION, parent_known=True)
        self.actor = actor_key(self.identity)
        self.coordinator.register(self.identity, "Alex")

    def tearDown(self):
        self.ledger.close()
        self.home.cleanup()

    def test_spawned_subagent_registration_has_no_enrollment_or_server_effect(self):
        from dataclasses import asdict
        session = "33333333-3333-4333-8333-333333333333"
        identity = NativeIdentity("codex", session, parent_session_id=SESSION, parent_known=True,
            evidence={"codex_thread_source": {"subAgent": {"thread_spawn": {
                "parent_thread_id": SESSION, "depth": 1}}},
                "codex_thread_source_session_id": session,
                "codex_thread_source_origin": "app-server:thread/read"})
        before = list(self.backend.register_calls)
        for value in (identity, asdict(identity)):
            with self.subTest(identity_type=type(value).__name__), self.assertRaisesRegex(ValueError, "subagents"):
                self.coordinator.register(value, "Spawned peer")
            with self.assertRaises(LedgerError):
                self.ledger.actor(identity.key)
            self.assertEqual(self.backend.register_calls, before)
        self.coordinator.register(identity, "Manual peer", mode="manual")
        self.assertEqual(self.ledger.actor(identity.key)["mode"], "manual")

    def hook(self, event, *, prompt=None, token=None):
        native_event = "UserPromptSubmit" if event == "submit" else "Stop"
        captured = HookEvent(self.identity, event, native_event, TURN, prompt=prompt, wake_token=token)
        with patch("murmur.native.parse_hook", return_value=captured):
            return hooks.handle(self.ledger, "codex", native_event, {"prompt": prompt or ""})

    def test_doorbell_admission_selective_read_reply_and_completion(self):
        self.backend.add("question", causal={"chain_ids": ["review-exchange"]})
        self.backend.add("reply-context", when=102, root="question", causal={"chain_ids": ["review-exchange"]})
        self.coordinator.sync(self.actor)
        delivery = self.coordinator.deliver(self.identity)
        self.assertEqual(delivery.status, "accepted")
        message = self.transport.calls[0][1]
        token = parse_wake(message)
        self.assertEqual(message, "You have new messages in ‘Build review’. [wake:" + token + "]")
        self.assertNotIn("review-exchange", message)
        self.assertTrue(all(p["announced_at"] is None for p in self.ledger.unread_posts(self.actor)))
        self.assertEqual(self.hook("submit", prompt=message, token=token), {})
        self.assertEqual(self.ledger.active_turn(self.actor)["turn_key"], TURN)
        self.assertEqual(self.ledger.attempt(token)["status"], "admitted")
        self.assertEqual(self.coordinator.inbox(self.actor)[0]["unread_count"], 2)
        self.assertEqual(self.coordinator.inbox(self.actor)[0]["preview"], "Question about reply-context")
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 2)
        selected = self.coordinator.read(self.actor, "build", root="question", limit=1)
        self.assertEqual([p["id"] for p in selected], ["question"])
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 2)
        self.backend.add("concurrent", when=103)
        self.coordinator.sync(self.actor)
        self.ledger.mark_read(self.actor, [p["id"] for p in selected])
        self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["reply-context", "concurrent"])
        self.coordinator.mutate(self.actor, "post", channel_id="build", root_id="question", message="Useful findings")
        payload = self.backend.mutation_calls[0][3]
        self.assertEqual(payload["root_id"], "question")
        self.assertEqual(payload["causal"], {"chain_ids": ["review-exchange"]})
        self.assertEqual(self.ledger.outbounds()[0]["state"], "complete")
        self.assertEqual(self.hook("complete"), {})
        self.assertIsNone(self.ledger.active_turn(self.actor))
        self.assertEqual(self.ledger.causal_context(self.actor), {"chain_ids": ["review-exchange"]})

    def test_wrong_actual_channel_or_thread_is_rejected_without_read_ack(self):
        self.backend.add("question")
        self.coordinator.sync(self.actor)
        self.backend.canonical["question"]["channel_id"] = "secret"
        with self.assertRaisesRegex(RuntimeError, "wrong conversation"):
            self.coordinator.read(self.actor, "build")
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 1)
        self.backend.canonical["question"]["channel_id"] = "build"
        self.backend.add("reply", when=102, root="question")
        self.coordinator.sync(self.actor)
        self.backend.canonical["reply"]["root_id"] = "unrelated-root"
        with self.assertRaisesRegex(RuntimeError, "wrong thread"):
            self.coordinator.read(self.actor, "build", root="question")
        self.backend.canonical["reply"]["channel_id"] = "secret"
        self.backend.canonical["reply"]["root_id"] = "question"
        with self.assertRaisesRegex(RuntimeError, "another conversation"):
            self.coordinator.history(self.actor, "build", root="question")
        self.assertEqual(len(self.ledger.unread_posts(self.actor)), 2)

    def test_lost_post_response_is_persisted_then_reconciled_without_resending(self):
        self.backend.lose_response = True
        seen = []
        self.backend.before_mutation = lambda op_id: seen.append(self.ledger.outbound(op_id)["state"])
        with self.assertRaises(BackendError):
            self.coordinator.mutate(self.actor, "post", channel_id="build", message="Once")
        self.assertEqual(seen, ["pending"])
        operation = self.ledger.outbounds()[0]
        self.assertEqual(operation["state"], "uncertain")
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=lambda: 100)
        self.coordinator = Coordinator(self.ledger, self.backend, self.transport)
        self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(len(self.backend.mutation_calls), 1)
        completed = self.ledger.outbound(operation["op_id"])
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(completed["result"], {"post_id": "reply-" + operation["op_id"], "channel_id": "build"})
        self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(self.backend.operation_calls, [operation["op_id"]])

    def test_absent_server_operation_remains_uncertain_without_automatic_repost(self):
        self.backend.lose_response = True
        self.backend.commit_before_failure = False
        with self.assertRaises(BackendError):
            self.coordinator.mutate(self.actor, "post", channel_id="build", message="Maybe sent")
        self.coordinator.reconcile_outbound(self.actor)
        self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(len(self.backend.mutation_calls), 1)
        self.assertEqual(self.ledger.outbounds()[0]["state"], "uncertain")

    def test_owned_operation_metadata_reconciles_after_conversation_departure(self):
        self.backend.lose_response = True
        with self.assertRaises(BackendError):
            self.coordinator.mutate(self.actor, "post", channel_id="build", message="Before departure")
        operation = self.ledger.outbounds()[0]
        self.backend.chats = []
        self.ledger.sync_conversations(self.actor, [])
        self.backend.operations[operation["op_id"]] = {"status": "complete", "result": {
            "post_id": "canonical-post", "channel_id": "build"}}
        self.coordinator.reconcile_outbound(self.actor)
        self.assertEqual(len(self.backend.mutation_calls), 1)
        completed = self.ledger.outbound(operation["op_id"])
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(completed["result"], {"post_id": "canonical-post", "channel_id": "build"})

    def test_registration_with_unresolved_ancestry_provisions_nothing(self):
        unknown = NativeIdentity("codex", "33333333-3333-4333-8333-333333333333")
        calls = list(self.backend.register_calls)
        with self.assertRaisesRegex(RuntimeError, "ancestry is unresolved"):
            self.coordinator.register(unknown, "Unproved fork")
        self.assertEqual(self.backend.register_calls, calls)
        with self.assertRaises(LedgerError):
            self.ledger.actor(actor_key(unknown))

    def test_cli_flush_failure_does_not_consume_messages(self):
        class BrokenOutput(io.StringIO):
            def flush(self):
                raise BrokenPipeError("Consumer disappeared")

        self.backend.add("question")
        with patch("murmur.__main__.config.ledger_path", return_value=self.path), \
             patch("murmur.__main__.config.load", return_value={"url": "http://fake", "token": "secret"}), \
             patch("murmur.__main__.Backend", return_value=self.backend), \
             patch("murmur.native.current_identity", return_value=self.identity):
            with redirect_stdout(BrokenOutput()), self.assertRaises(BrokenPipeError):
                cli.main(["read", "build", "--limit", "1"])
            self.assertEqual([p["id"] for p in self.ledger.unread_posts(self.actor)], ["question"])
            with redirect_stdout(io.StringIO()) as output:
                cli.main(["read", "build", "--limit", "1"])
            self.assertIn('"id": "question"', output.getvalue())
            self.assertEqual(self.ledger.unread_posts(self.actor), [])


if __name__ == "__main__":
    unittest.main()
