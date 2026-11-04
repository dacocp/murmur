"""Named arrivals freeze intent before dispatch and do not enroll identities."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import workflow
from ops_files import private_json


class IncomingWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.channel, self.root, self.post = "c" * 26, "r" * 26, "p" * 26
        self.actor = "codex:synthetic-existing"
        self.fixture = SimpleNamespace(state=self.state, origin="http://murmur-preview.localhost:18069",
                                       management_origin="http://127.0.0.1:18069",
                                       admin={"token": "fixture-human", "user_id": "h" * 26},
                                       coordinator={"token": "fixture-coordinator"})
        private_json(self.state / "reader-seed.json", {"origin": self.fixture.origin, "synthetic": True,
                     "channel": self.channel, "agents": [self.actor]})
        self.seed = (self.state / "reader-seed.json").read_bytes()
        self.calls, self.fail_once = [], False
        outer = self

        class FakeClient:
            def __init__(self, origin, token):
                self.origin, self.token, self.user_id = origin, token, None

            def json(self, path, data=None, **options):
                outer.calls.append((path, data))
                if path.endswith("/bootstrap"):
                    result = {"user": {"id": "h" * 26, "username": "fixture-human"},
                              "conversations": [{"id": outer.channel, "members": [outer.actor]}]}
                elif "/context/" in path:
                    result = {"root": {"id": outer.root}, "channel_id": outer.channel}
                elif path.endswith("/dms"):
                    result = {"conversation": {"id": outer.channel}}
                elif path == "/plugins/murmur/v1/posts":
                    stored = json.loads(next((outer.state / "workflow-events").glob("*.json")).read_text())
                    if stored["post_op"] != data["op_id"]:
                        raise AssertionError("Operation was not persisted before the request")
                    if outer.fail_once:
                        outer.fail_once = False
                        raise TimeoutError("Discarded fixture response")
                    result = {"post": {"id": outer.post, "channel_id": outer.channel, "root_id": data["root_id"]}}
                else:
                    raise AssertionError("Unexpected fixture action: " + path)
                return result, {"Cache-Control": "private, no-store"}

        self.client = patch.object(workflow, "Client", FakeClient)
        self.client.start()
        self.addCleanup(self.client.stop)

    def posts(self):
        return [data for path, data in self.calls if path == "/plugins/murmur/v1/posts"]

    def test_same_named_event_returns_one_receipt_and_rejects_changed_intent(self):
        first = workflow.arrive(self.fixture, "reply-one", "routine", root=self.root)
        again = workflow.arrive(self.fixture, "reply-one", "routine", root=self.root)
        self.assertEqual(first, again)
        self.assertEqual(len(self.posts()), 1)
        with self.assertRaisesRegex(ValueError, "immutable"):
            workflow.arrive(self.fixture, "reply-one", "routine", root=self.root, text="Changed content")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual((self.state / "reader-seed.json").read_bytes(), self.seed)
        self.assertFalse(any("register" in path for path, _ in self.calls))

    def test_unknown_response_recovery_keeps_the_original_operation(self):
        self.fail_once = True
        with self.assertRaises(TimeoutError):
            workflow.arrive(self.fixture, "mention-one", "mention", root=self.root)
        result = workflow.arrive(self.fixture, "mention-one", "mention", root=self.root)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.posts()[0], self.posts()[1])
        self.assertEqual(result["op_id"], self.posts()[0]["op_id"])
        self.assertIn("@fixture-human", self.posts()[0]["message"])

    def test_direct_contact_opens_only_human_dm_and_uses_fresh_root(self):
        result = workflow.arrive(self.fixture, "direct-one", "direct")
        dm = next(data for path, data in self.calls if path.endswith("/dms"))
        self.assertEqual(dm["agent_id"], self.actor)
        self.assertEqual(self.posts()[0]["root_id"], "")
        self.assertEqual(result["root_id"], self.post)
        with self.assertRaisesRegex(ValueError, "existing synthetic"):
            workflow.arrive(self.fixture, "foreign", "direct", actor="codex:not-a-fixture-actor")


if __name__ == "__main__":
    unittest.main()
