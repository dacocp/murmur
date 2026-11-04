"""The disposable server profile must not reuse the live installation."""

from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops_files import private_json, read_private_json, write_private
from server.fixture import fixture_origin, load_fixture
import release


SOURCE = Path(__file__).resolve().parents[1] / "server" / "bootstrap.py"
spec = importlib.util.spec_from_file_location("murmur_test_bootstrap", SOURCE)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class FixtureOriginTests(unittest.TestCase):
    def test_reader_alias_requires_exact_hostname_and_bound_test_port(self):
        for value in ("http://127.0.0.1:18065", "http://murmur-preview.localhost:18065",
                      "https://fixture.example.ts.net:18443"):
            self.assertEqual(fixture_origin(value, 18065), value)
        for value in ("http://murmur-preview.localhost:18066", "http://murmur-preview.localhost:8065",
                      "http://murmur-preview.localhost", "http://other.localhost:18065",
                      "http://localhost:18065", "http://murmur-preview.localhost.example:18065",
                      "http://user:secret@murmur-preview.localhost:18065",
                      "http://murmur-preview.localhost:18065/path",
                      "http://murmur-preview.localhost:18065?query=1",
                      "http://murmur-preview.localhost:18065#fragment", "http://[invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fixture_origin(value, 18065)


class IsolatedBootstrapTests(unittest.TestCase):
    def invoke(self, *args):
        with patch("sys.argv", [str(SOURCE), *args]), redirect_stderr(io.StringIO()):
            bootstrap.main()

    def test_rejects_live_port_project_and_partial_profiles_before_commands(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bootstrap.subprocess, "run") as run:
            state = Path(directory) / "not-created"
            for arguments in (
                ["--test-project", "murmur"],
                ["--test-port", "18065"],
                ["--test-site-url", "https://example.test"],
                ["--test-project", "murmur", "--test-port", "18065", "--test-state", str(state)],
                ["--test-project", "murmur-test-safe", "--test-port", "8065", "--test-state", str(state)],
            ):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    self.invoke(*arguments)
            self.assertFalse(state.exists())
            run.assert_not_called()

    def test_invalid_https_origins_are_rejected_before_state_or_commands(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bootstrap.subprocess, "run") as run:
            state = Path(directory) / "not-created"
            for origin in ("http://example.test", "https://user:secret@example.test",
                           "https://example.test/subpath", "https://example.test?secret=x",
                           "https://example.test#fragment", "https://example.test:invalid",
                           "https://example.test\n", "https://exa mple.test"):
                with self.subTest(origin=origin), self.assertRaises(SystemExit):
                    self.invoke("--test-project", "murmur-test-safe", "--test-port", "18065",
                                "--test-state", str(state), "--test-site-url", origin)
            self.assertFalse(state.exists())
            run.assert_not_called()

    def test_unmarked_credentials_are_never_reused_or_modified(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bootstrap.subprocess, "run") as run:
            path = Path(directory) / "credentials.json"
            path.write_text('{"existing":"installation"}')
            with self.assertRaises(SystemExit):
                self.invoke("--test-project", "murmur-test-safe", "--test-port", "18065",
                            "--test-state", directory)
            self.assertEqual(path.read_text(), '{"existing":"installation"}')
            self.assertFalse((Path(directory) / ".env").exists())
            run.assert_not_called()

    def test_server_directory_and_unmarked_existing_env_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bootstrap.subprocess, "run") as run:
            server = Path(directory) / "server"
            server.mkdir()
            env = server / ".env"
            env.write_text("EXISTING_DATABASE_PASSWORD=private\n")
            for here in (server, Path(directory) / "other-source"):
                with self.subTest(here=here), patch.object(bootstrap, "HERE", here), \
                        self.assertRaises(SystemExit):
                    self.invoke("--test-project", "murmur-test-safe", "--test-port", "18065",
                                "--test-state", str(server))
            self.assertEqual(env.read_text(), "EXISTING_DATABASE_PASSWORD=private\n")
            self.assertFalse((server / "test-installation.json").exists())
            run.assert_not_called()


class OperationalBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "checkout/tools/murmur"
        self.server = self.source / "server"
        (self.source / "murmur").mkdir(parents=True)
        (self.source / "murmur/__init__.py").write_text("")
        skill = self.source.parents[1] / "skills/murmur"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("fixture skill")
        (self.server / "dist").mkdir(parents=True)
        (self.server / "compose.yaml").write_text("fixture compose source")
        self.archive = self.server / "dist/murmur-0.1.0.tar.gz"
        self.archive.write_bytes(b"fixture archive")
        release.build_manifest(self.source, self.server / "dist/build-manifest.json")
        self.artifact = release.finish_build(self.source, self.archive)
        self.state = self.root / "permanent"
        self.calls = []
        self.config = {"TeamSettings": {}, "PluginSettings": {"Plugins": {}}}
        self.config_updates = []
        self.resources = ([], [])
        self.records = []
        self.resolved = {"name": "murmur", "services": {"mattermost": {
            "ports": [{"target": 8065, "published": "8065", "host_ip": "127.0.0.1", "protocol": "tcp"}],
            "environment": {"MM_SERVICESETTINGS_SITEURL": "http://localhost:8065"}}}}

    def output(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:3] == ["docker", "ps", "-aq"]:
            return "\n".join(self.resources[0])
        if args[:3] == ["docker", "volume", "ls"]:
            return "\n".join(self.resources[1])
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(self.records)
        if args[-3:] == ["config", "--format", "json"]:
            return json.dumps(self.resolved)
        raise AssertionError(args)

    def request(self, path, *args, **kwargs):
        if path == "/api/v4/config":
            if not args:
                return json.loads(json.dumps(self.config))
            if args[0] == "PUT":
                self.config_updates.append(json.loads(json.dumps(args[1])))
        if path.endswith("/health"):
            return {"version": "0.1.0", "build": {key: self.artifact[key]
                    for key in ("revision", "source_sha256", "compatibility")}}
        return {}

    def invoke(self, *args):
        response = io.BytesIO(json.dumps({"id": "fixture-admin"}).encode())
        response.headers = {"Token": "fixture-admin-token"}
        with patch.object(bootstrap, "HERE", self.server), \
                patch.object(bootstrap.subprocess, "check_output", side_effect=self.output), \
                patch.object(bootstrap.subprocess, "run", side_effect=lambda a, **k: self.calls.append((a, k))), \
                patch.object(bootstrap, "request", side_effect=self.request), \
                patch.object(bootstrap, "urlopen", return_value=response), \
                patch.object(release, "source_manifest", wraps=release.source_manifest), \
                patch.dict(os.environ, {"COMPOSE_FILE": "/foreign/config", "COMPOSE_PROJECT_NAME": "foreign", "MURMUR_DB_PASSWORD": "foreign"}), \
                redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            bootstrap.main(["--no-build", *args])

    def test_private_writer_tightens_existing_modes_and_preserves_on_replace_failure(self):
        path = self.root / "session.json"
        path.write_text("old private value")
        path.chmod(0o644)
        private_json(path, {"token": "fixture"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = path.read_bytes()
        with patch("ops_files.os.replace", side_effect=OSError("injected interruption")):
            with self.assertRaises(OSError):
                private_json(path, {"token": "replacement"})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".murmur-*")), [])
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "chmod 600"):
            read_private_json(path)
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError, "regular file"):
            private_json(link, {"token": "never written"})

    def test_successful_permanent_setup_binds_explicit_compose_and_ignores_ambient_selectors(self):
        # Avoid the Git metadata subprocess: source content itself is measured.
        manifest = release.source_manifest(self.source)
        with patch.object(release, "source_manifest", return_value=manifest):
            self.invoke("--state", str(self.state), "--project", "murmur", "--port", "8065")
        marker = read_private_json(self.state / "installation.json")
        self.assertEqual(marker["project"], "murmur")
        self.assertEqual(marker["build"]["compatibility"], release.COMPATIBILITY)
        up = next((a, k) for a, k in self.calls if a[-2:] == ["up", "-d"])
        self.assertIn(str(self.state / "compose.json"), up[0])
        self.assertEqual(up[1]["cwd"], self.state)
        self.assertFalse(any(key.startswith("COMPOSE_") for key in up[1]["env"]))
        self.assertNotIn("MURMUR_DB_PASSWORD", up[1]["env"])
        self.assertFalse((self.server / ".state").exists())
        self.assertFalse((self.server / ".env").exists())
        self.assertEqual(self.config_updates[-1]["PluginSettings"]["Plugins"]["murmur"],
                         {"coordinatortoken": read_private_json(self.state / "credentials.json")["coordinator_token"]})

    def test_rerun_preserves_operator_and_unknown_plugin_settings(self):
        masked_secret = "*" * 32  # Mattermost model.FakeSetting, desanitized by Store.Set.
        existing = {"coordinatortoken": masked_secret, "operatoruserid": "fixture-human-operator",
                    "unknown": {"nested": ["preserve", 7]}, "unknownsecret": masked_secret}
        self.config = {"TeamSettings": {"MaxUsersPerTeam": 50, "OtherSetting": "preserve"},
                       "PluginSettings": {"Plugins": {"murmur": existing,
                                                     "other-plugin": {"unknown": "preserve"}}},
                       "OtherSection": {"unknown": "preserve"}}
        manifest = release.source_manifest(self.source)
        for _ in range(2):
            with patch.object(release, "source_manifest", return_value=manifest):
                self.invoke("--state", str(self.state), "--project", "murmur", "--port", "8065")
            token = read_private_json(self.state / "credentials.json")["coordinator_token"]
            update = self.config_updates[-1]
            self.assertEqual(update["PluginSettings"]["Plugins"]["murmur"],
                             {**existing, "coordinatortoken": token})
            self.assertEqual(update["PluginSettings"]["Plugins"]["other-plugin"], {"unknown": "preserve"})
            self.assertEqual(update["OtherSection"], self.config["OtherSection"])
            self.assertEqual(update["TeamSettings"], {"MaxUsersPerTeam": 1000, "OtherSetting": "preserve"})

    def test_unbound_existing_project_cannot_generate_replacement_credentials(self):
        self.resources = (["existing-container"], ["murmur_postgres"])
        with patch.object(bootstrap.secrets, "token_urlsafe", side_effect=AssertionError("must not replace credentials")):
            with self.assertRaises(SystemExit):
                self.invoke("--state", str(self.state), "--project", "murmur")
        self.assertFalse(self.state.exists())
        self.assertFalse(any(a[-2:] == ["up", "-d"] for a, _ in self.calls))

    def test_fresh_permanent_override_retains_lan_ports_origin_and_cors(self):
        override = self.root / "selected-lan.yaml"
        override.write_text("explicit LAN selection")
        service = self.resolved["services"]["mattermost"]
        service["ports"].append({"target": 8065, "published": "8065", "host_ip": "10.0.1.119", "protocol": "tcp"})
        service["environment"] = {"MM_SERVICESETTINGS_SITEURL": "http://10.0.1.119:8065",
            "MM_SERVICESETTINGS_ALLOWCORSFROM": "http://127.0.0.1:8065 http://localhost:8065"}
        manifest = release.source_manifest(self.source)
        for explicit_site in (None, "https://chosen.example.ts.net"):
            with self.subTest(explicit_site=explicit_site):
                state = self.root / ("fresh-lan" if explicit_site is None else "fresh-https")
                flags = ["--state", str(state), "--project", "murmur", "--compose-override", str(override)]
                if explicit_site:
                    flags += ["--site-url", explicit_site]
                with patch.object(release, "source_manifest", return_value=manifest):
                    self.invoke(*flags)
                saved = json.loads((state / "compose.json").read_text())["services"]["mattermost"]
                self.assertEqual(saved["ports"], service["ports"])
                self.assertEqual(saved["environment"]["MM_SERVICESETTINGS_ALLOWCORSFROM"],
                                 service["environment"]["MM_SERVICESETTINGS_ALLOWCORSFROM"])
                site = explicit_site or "http://10.0.1.119:8065"
                self.assertEqual(saved["environment"]["MM_SERVICESETTINGS_SITEURL"], site)
                self.assertEqual(read_private_json(state / "installation.json")["site_url"], site)
                self.assertEqual(read_private_json(state / "coordinator.json")["server_url"], site + "/plugins/murmur/v1")
        selected = [a for a, _ in self.calls if a[-3:] == ["config", "--format", "json"]]
        self.assertTrue(all(str(override) in args for args in selected))

    def test_fresh_permanent_override_without_selected_management_loopback_is_held(self):
        override = self.root / "lan-only.yaml"
        override.write_text("explicit LAN only selection")
        self.resolved["services"]["mattermost"]["ports"] = [
            {"target": 8065, "published": "8065", "host_ip": "10.0.1.119", "protocol": "tcp"}]
        manifest = release.source_manifest(self.source)
        with patch.object(release, "source_manifest", return_value=manifest), self.assertRaises(SystemExit):
            self.invoke("--state", str(self.state), "--project", "murmur", "--compose-override", str(override))
        self.assertFalse((self.state / "compose.json").exists())
        self.assertFalse(any(a[-2:] == ["up", "-d"] for a, _ in self.calls))

    def test_disposable_profile_replaces_every_resolved_port_and_ignores_lan_override(self):
        (self.server / "compose.override.yaml").write_text("unselected developer LAN override")
        self.resolved["services"]["mattermost"]["ports"].append(
            {"target": 8065, "published": "8065", "host_ip": "10.0.1.119", "protocol": "tcp"})
        manifest = release.source_manifest(self.source)
        with patch.object(release, "source_manifest", return_value=manifest):
            self.invoke("--test-state", str(self.state), "--test-project", "murmur-test-lan-isolation",
                        "--test-port", "18065", "--test-site-url", "https://fixture.example.ts.net:18443")
        saved = json.loads((self.state / "compose.json").read_text())["services"]["mattermost"]
        self.assertEqual(saved["ports"], [{"target": 8065, "published": "18065", "host_ip": "127.0.0.1", "protocol": "tcp"}])
        self.assertEqual(saved["environment"]["MM_SERVICESETTINGS_SITEURL"], "https://fixture.example.ts.net:18443")
        selected = next(a for a, _ in self.calls if a[-3:] == ["config", "--format", "json"])
        self.assertNotIn(str(self.server / "compose.override.yaml"), selected)

    def test_reader_demo_origin_keeps_the_existing_loopback_binding(self):
        manifest = release.source_manifest(self.source)
        with patch.object(release, "source_manifest", return_value=manifest):
            self.invoke("--test-state", str(self.state), "--test-project", "murmur-test-reader",
                        "--test-port", "18065", "--test-site-url", "http://murmur-preview.localhost:18065")
        saved = json.loads((self.state / "compose.json").read_text())["services"]["mattermost"]
        self.assertEqual(saved["ports"], [{"target": 8065, "published": "18065", "host_ip": "127.0.0.1", "protocol": "tcp"}])
        self.assertEqual(saved["environment"]["MM_SERVICESETTINGS_SITEURL"], "http://murmur-preview.localhost:18065")
        self.assertEqual(load_fixture(self.state).origin, "http://murmur-preview.localhost:18065")

    def test_checked_legacy_adoption_preserves_credentials_lan_origin_ports_and_cors(self):
        legacy = self.root / "old-checkout/server/.state"
        legacy.mkdir(parents=True, mode=0o700)
        creds = {"admin": "murmur-admin", "password": "retained-password",
                 "database_password": "retained-db-password", "coordinator_token": "retained-coordinator"}
        private_json(legacy / "credentials.json", creds)
        write_private(legacy.parent / ".env", b"MURMUR_DB_PASSWORD=retained-db-password\n")
        (legacy.parent / "compose.yaml").write_text("existing compose")
        override = legacy.parent / "compose.override.yaml"
        override.write_text("existing LAN override")
        self.resources = (["pg-id", "mm-id"], ["murmur_" + name for name in bootstrap.VOLUMES])
        for name, image in (("postgres", "postgres:16"), ("mattermost", "mattermost/mattermost-team-edition:12.0.1")):
            self.records.append({"Config": {"Image": image,
                "Labels": {"com.docker.compose.project": "murmur", "com.docker.compose.service": name},
                "Env": ["POSTGRES_PASSWORD=retained-db-password"]},
                "HostConfig": {"PortBindings": {"8065/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8065"}]}}})
        self.resolved["services"]["mattermost"]["environment"] = {
            "MM_SERVICESETTINGS_SITEURL": "http://10.0.1.119:8065",
            "MM_SERVICESETTINGS_ALLOWCORSFROM": "http://127.0.0.1:8065 http://localhost:8065"}
        self.resolved["services"]["mattermost"]["ports"].append(
            {"target": 8065, "published": "8065", "host_ip": "10.0.1.119", "protocol": "tcp"})
        manifest = release.source_manifest(self.source)
        with patch.object(release, "source_manifest", return_value=manifest), \
                patch.object(bootstrap.secrets, "token_urlsafe", side_effect=AssertionError("must preserve credentials")):
            self.invoke("--state", str(self.state), "--project", "murmur", "--adopt-legacy", str(legacy))
        self.assertEqual(read_private_json(self.state / "credentials.json"), creds)
        config = json.loads((self.state / "compose.json").read_text())
        self.assertEqual(config, self.resolved)
        marker = read_private_json(self.state / "installation.json")
        self.assertEqual(marker["site_url"], "http://10.0.1.119:8065")
        coordinator = read_private_json(self.state / "coordinator.json")
        self.assertEqual(coordinator["server_url"], "http://10.0.1.119:8065/plugins/murmur/v1")
        selected = next(a for a, _ in self.calls if a[-3:] == ["config", "--format", "json"])
        self.assertIn(str(override), selected)
        self.assertEqual(read_private_json(legacy / "credentials.json"), creds)

    def test_stale_build_and_pre_sql_manifest_are_rejected(self):
        (self.source / "murmur/__init__.py").write_text("changed source")
        with self.assertRaisesRegex(ValueError, "does not match"):
            release.check_build(self.source, self.archive)
        changed = dict(self.artifact, compatibility={"metadata_backend": "kv-v1"})
        private_json(self.server / "dist/build-manifest.json", changed)
        with self.assertRaisesRegex(ValueError, "compatibility"):
            release.check_build(self.source, self.archive)


class FixtureBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.origin = "http://127.0.0.1:18065"
        self.marker = {"project": "murmur-test-fixture", "port": 18065}
        private_json(self.state / "test-installation.json", self.marker)
        self.config = {"server_url": self.origin + "/plugins/murmur/v1", "token": "fixture"}
        private_json(self.state / "coordinator.json", self.config)
        private_json(self.state / "admin-session.json", {"token": "fixture-admin", "user_id": "fixture-user"})
        environment = patch.dict(os.environ, {key: "" for key in ("MURMUR_TEST_STATE", "MURMUR_INTEGRATION_CONFIG",
            "MURMUR_TEST_PROJECT", "MURMUR_TEST_ORIGIN", "MURMUR_TEST_HTTPS_ORIGIN")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_marker_alone_binds_project_and_origin_and_never_falls_back(self):
        with patch.dict(os.environ, {"MURMUR_TEST_STATE": str(self.state)}):
            fixture = load_fixture(require_admin=True)
        self.assertEqual((fixture.project, fixture.origin), ("murmur-test-fixture", self.origin))
        self.assertEqual(fixture.admin["user_id"], "fixture-user")
        with patch.dict(os.environ, {"MURMUR_INTEGRATION_CONFIG": str(self.state / "coordinator.json")}):
            with self.assertRaisesRegex(ValueError, "Select a marked"):
                load_fixture()

    def test_redirected_config_insecure_origin_or_disagreeing_override_is_rejected(self):
        for url in ("http://127.0.0.1:8065/plugins/murmur/v1", "https://other.example/plugins/murmur/v1"):
            private_json(self.state / "coordinator.json", {**self.config, "server_url": url})
            with self.assertRaisesRegex(ValueError, "URL disagrees"):
                load_fixture(self.state)
        private_json(self.state / "coordinator.json", self.config)
        with self.assertRaisesRegex(ValueError, "disagrees"):
            load_fixture(self.state, project="murmur-test-other")
        self.marker["site_url"] = "http://remote.example:18065"
        private_json(self.state / "test-installation.json", self.marker)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            load_fixture(self.state)

    def test_bound_https_fixture_supported_and_private_modes_required(self):
        self.marker["site_url"] = "https://fixture.example.ts.net:18443"
        private_json(self.state / "test-installation.json", self.marker)
        private_json(self.state / "coordinator.json", {**self.config, "server_url": self.marker["site_url"] + "/plugins/murmur/v1"})
        fixture = load_fixture(self.state)
        self.assertEqual(fixture.origin, self.marker["site_url"])
        (self.state / "test-installation.json").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "chmod 600"):
            load_fixture(self.state)

    def test_http_fixture_helpers_use_explicit_current_protocol_and_registration_ids(self):
        path = SOURCE.parent / "integration_test.py"
        spec = importlib.util.spec_from_file_location("isolated_http_helpers", path)
        helper = importlib.util.module_from_spec(spec)
        # Import must not read normal installation files or perform requests.
        with patch("ops_files.read_private_json", side_effect=AssertionError("No import-time settings reads")):
            spec.loader.exec_module(helper)
        captured = []
        def call(path, data=None, **kwargs):
            captured.append((path, data))
            return {"agent": {"id": data["actor"], "user_id": "user-" + data["actor"]}} if data else {}
        with patch.object(helper, "call", side_effect=call):
            parent = helper.agent("parent fixture")
            child = helper.agent("child fixture", parent)
            helper.feed(child)
        self.assertEqual(captured[0][1]["mode"], "native")
        self.assertEqual(captured[0][1]["lineage"], [{"actor": parent, "parent": None}])
        self.assertEqual(captured[1][1]["lineage"], [{"actor": child, "parent": parent}, {"actor": parent, "parent": None}])
        self.assertEqual(helper.agent_info(child)["user_id"], "user-" + child)
        self.assertIn("attention_version=3", captured[2][0])
        self.assertFalse(any(path.endswith("/agents") for path, _ in captured))



if __name__ == "__main__":
    unittest.main()
