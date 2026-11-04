"""Qualification gates cannot dispatch writes or reuse a stale build verdict."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import workspace_acceptance as acceptance
from ops_files import private_directory, private_json


class QualificationGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.fixture = SimpleNamespace(state=self.state, origin="http://murmur-preview.localhost:18069",
            management_origin="http://127.0.0.1:18069", admin={"token": "test-admin", "user_id": "h" * 26},
            coordinator={"token": "test-coordinator"})
        private_json(self.state / "reader-seed.json", {"origin": self.fixture.origin, "synthetic": True,
                                                       "agents": ["existing-a", "existing-b"]})
        private_json(self.state / "test-installation.json", {"build": {"source_sha256": "current-source"}})
        self.calls = []
        outer = self

        class ReadOnlyFixture:
            def __init__(self, origin, token=None):
                self.origin, self.user_id = origin, None

            def json(self, path, data=None, **options):
                outer.calls.append(path)
                if data is not None:
                    raise AssertionError("Preflight dispatched a write")
                if path == "/plugins/murmur/v1/health":
                    return {"build": {"source_sha256": "current-source"}}, {}
                if path == "/plugins/murmur/operator/v1/bootstrap":
                    return {"capabilities": {"personal_workspace": False, "own_messages": True}}, {"Cache-Control": "private, no-store"}
                raise AssertionError("Unexpected request before capability gate: " + path)

        self.client = patch.object(acceptance, "Client", ReadOnlyFixture)
        self.loader = patch.object(acceptance, "load_fixture", return_value=self.fixture)
        self.client.start(); self.loader.start()
        self.addCleanup(self.client.stop); self.addCleanup(self.loader.stop)

    def invoke(self):
        with patch("sys.argv", ["workspace_acceptance.py", "--test-state", str(self.state), "--run", "guard"]), redirect_stderr(io.StringIO()):
            acceptance.main()

    def test_old_backend_fails_before_any_account_channel_or_post_write(self):
        with self.assertRaises(SystemExit) as error:
            self.invoke()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.calls, ["/plugins/murmur/v1/health", "/plugins/murmur/operator/v1/bootstrap"])

    def test_completed_result_for_another_build_cannot_claim_current_acceptance(self):
        home = private_directory(private_directory(self.state / "workspace-acceptance") / "guard")
        private_json(home / "result.json", {"complete": True, "backend_build": {"source_sha256": "old-source"}})
        with self.assertRaises(SystemExit) as error:
            self.invoke()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.calls, ["/plugins/murmur/v1/health"])


class HumanFixtureCreationTests(unittest.TestCase):
    def test_native_error_identifier_is_never_treated_as_a_user_id(self):
        run = acceptance.Run.__new__(acceptance.Run)
        run.fixture = SimpleNamespace(origin="http://murmur-preview.localhost:18069", management_origin="http://127.0.0.1:18069")
        run.plan = {"humans": [{"username": "fixture-human-a", "password": "dummy-fixture-password",
                                "id": "app.user.get_by_username.app_error"}]}
        run.sessions, calls = [], []
        run.save = lambda: None

        class NativeAdmin:
            def json(self, path, data=None, **options):
                calls.append((path, data))
                if path.startswith("/api/v4/users/username/"):
                    return {"id": "app.user.get_by_username.app_error"}, {}
                if path == "/api/v4/users":
                    return {"id": "a" * 26}, {}
                if path == "/api/v4/users/" + "a" * 26:
                    return {"id": "a" * 26, "username": "fixture-human-a", "roles": "system_user"}, {}
                raise AssertionError("An error ID escaped into a native user route")

            def call(self, path, data, **options):
                calls.append((path, data))
                if path != "/api/v4/users/" + "a" * 26 + "/roles":
                    raise AssertionError("An error ID escaped into a role change")

        class Login:
            def __init__(self, origin):
                self.origin = origin

            def json(self, path, data):
                return {"id": "a" * 26}, {"Token": "dummy-new-session"}

        run.admin = NativeAdmin()
        with patch.object(acceptance, "Client", Login):
            user = run.human(0)
        self.assertEqual(user.user_id, "a" * 26)
        self.assertEqual(run.plan["humans"][0]["id"], "a" * 26)
        self.assertTrue(any(path == "/api/v4/users" for path, _ in calls))
        self.assertFalse(any("app.user.get_by_username.app_error" in path for path, _ in calls))


if __name__ == "__main__":
    unittest.main()
