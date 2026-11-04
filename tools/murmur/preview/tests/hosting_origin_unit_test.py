"""Canonical demo-origin changes preserve the marked fixture's other state."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("preview_origin", Path(__file__).with_name("hosting_origin.py"))
origin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(origin)
from ops_files import private_json, write_private
from server.fixture import load_fixture
from hosting import proof_origin


class DemoOriginTests(unittest.TestCase):
    def test_demo_proof_uses_the_cookie_hostname_and_keeps_management_transport_explicit(self):
        fixture = SimpleNamespace(origin="http://murmur-preview.localhost:18069",
                                  management_origin="http://127.0.0.1:18069")
        self.assertEqual(proof_origin(fixture), fixture.origin)
        fixture.origin = "https://fixture.example.ts.net:18443"
        self.assertEqual(proof_origin(fixture), fixture.management_origin)

    def fixture(self, directory):
        state = Path(directory)
        self.port = 18069
        self.old = "http://127.0.0.1:18069"
        compose = {"services": {"mattermost": {"ports": [{"target": 8065, "published": "18069", "host_ip": "127.0.0.1", "protocol": "tcp"}],
                       "volumes": ["owned-config:/mattermost/config"],
                       "environment": {"MM_SERVICESETTINGS_SITEURL": self.old, "PRIVATE_SENTINEL": "retained"}},
                       "postgres": {"volumes": ["owned-db:/var/lib/postgresql/data"]}}}
        private_json(state / "compose.json", compose)
        private_json(state / "test-installation.json", {"project": "murmur-test-origin", "port": self.port,
                     "site_url": self.old, "compose_sha256": hashlib.sha256((state / "compose.json").read_bytes()).hexdigest(),
                     "build": {"archive_sha256": "previous-build"}})
        private_json(state / "coordinator.json", {"server_url": self.old + "/plugins/murmur/v1", "token": "retained-token"})
        private_json(state / "reader-seed-plan.json", {"origin": self.old, "actors": ["actor"], "operations": ["op"]})
        private_json(state / "reader-seed.json", {"origin": self.old, "older_exact_post": "post", "root_count": 55})
        private_json(state / "credentials.json", {"password": "retained-password"})
        private_json(state / "admin-session.json", {"token": "retained-session"})
        write_private(state / ".env", b"RETAINED_ENV=1\n")
        return load_fixture(state)

    def test_plan_and_rebound_metadata_keep_credentials_ids_ports_and_volumes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            before = {p.name: p.read_bytes() for p in fixture.state.iterdir()}
            target, updates = origin.origin_plan(fixture)
            self.assertEqual(target, "http://murmur-preview.localhost:18069")
            self.assertEqual(before, {p.name: p.read_bytes() for p in fixture.state.iterdir()})
            self.assertEqual({p.name for p in updates}, {"compose.json", "test-installation.json", "coordinator.json",
                                                         "reader-seed-plan.json", "reader-seed.json"})
            old_compose = json.loads(before["compose.json"])
            new_compose = json.loads(updates[fixture.state / "compose.json"])
            old_compose["services"]["mattermost"]["environment"]["MM_SERVICESETTINGS_SITEURL"] = target
            self.assertEqual(old_compose, new_compose)
            for path, content in updates.items():
                write_private(path, content)
            after = load_fixture(fixture.state)
            self.assertEqual(after.origin, target)
            self.assertEqual(after.management_origin, self.old)
            self.assertEqual(after.coordinator["token"], "retained-token")
            for name in ("credentials.json", "admin-session.json", ".env"):
                self.assertEqual((fixture.state / name).read_bytes(), before[name])
            for name in ("reader-seed-plan.json", "reader-seed.json"):
                expected = json.loads(before[name]); expected["origin"] = target
                self.assertEqual(json.loads((fixture.state / name).read_bytes()), expected)
            self.assertEqual(json.loads((fixture.state / "test-installation.json").read_text())["build"], {"archive_sha256": "previous-build"})
            again, second = origin.origin_plan(after)
            self.assertEqual(again, target)
            self.assertTrue(all(path.read_bytes() == content for path, content in second.items()))

    def test_widened_binding_or_mismatched_metadata_never_gets_an_origin_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(directory)
            compose = json.loads((fixture.state / "compose.json").read_text())
            compose["services"]["mattermost"]["ports"][0]["host_ip"] = "0.0.0.0"
            private_json(fixture.state / "compose.json", compose)
            with self.assertRaisesRegex(ValueError, "differs from its marker"):
                origin.origin_plan(fixture)
            marker = json.loads((fixture.state / "test-installation.json").read_text())
            marker["compose_sha256"] = hashlib.sha256((fixture.state / "compose.json").read_bytes()).hexdigest()
            private_json(fixture.state / "test-installation.json", marker)
            with self.assertRaisesRegex(ValueError, "loopback binding"):
                origin.origin_plan(fixture)


if __name__ == "__main__":
    unittest.main()
