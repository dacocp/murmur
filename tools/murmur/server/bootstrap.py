#!/usr/bin/env python3
"""Provision a bound Murmur server installation; never infer ownership from cwd.

Permanent state lives outside source. Existing checkout state must be deliberately
adopted with --adopt-legacy, retaining and verifying its credentials and resources.
Test installations require a marked murmur-test-* project and a non-live port.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from ops_files import private_directory, private_json, read_private_json, regular_file, write_private
from server.fixture import fixture_origin
import release

BASE = "http://127.0.0.1:8065"
VOLUMES = ("postgres", "mattermost-config", "mattermost-data", "mattermost-logs",
           "mattermost-plugins", "mattermost-client-plugins")


def origin(value, *, https=False):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ({"https"} if https else {"http", "https"}) and parsed.hostname
                 and not parsed.username and not parsed.password and parsed.path in {"", "/"}
                 and not parsed.query and not parsed.fragment
                 and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Expected an HTTP(S) origin without credentials, path, query or fragment")
    return value.rstrip("/")


def compose_environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith("COMPOSE_") and key != "MURMUR_DB_PASSWORD"}


def project_resources(project, env):
    ids = subprocess.check_output(["docker", "ps", "-aq", "--filter",
                                   "label=com.docker.compose.project=" + project], env=env, text=True).split()
    volumes = subprocess.check_output(["docker", "volume", "ls", "-q", "--filter",
                                       "label=com.docker.compose.project=" + project], env=env, text=True).split()
    return ids, volumes


def legacy_credentials(legacy, project, port, resources, env):
    """Read only explicitly selected legacy state; never create replacement keys."""
    private_directory(legacy)
    creds = read_private_json(legacy / "credentials.json")
    env_file = legacy.parent / ".env"
    regular_file(env_file, private=True)
    if env_file.read_text().strip() != "MURMUR_DB_PASSWORD=" + creds["database_password"]:
        raise ValueError("Legacy environment and database credential disagree; installation preserved")
    ids, volumes = resources
    expected_volumes = {project + "_" + name for name in VOLUMES}
    if not ids or set(volumes) != expected_volumes:
        raise ValueError("Legacy project resources cannot be verified; installation preserved")
    records = json.loads(subprocess.check_output(["docker", "inspect", *ids], env=env, text=True))
    services = {record["Config"]["Labels"].get("com.docker.compose.service"): record for record in records}
    if set(services) != {"postgres", "mattermost"}:
        raise ValueError("Legacy project has unexpected services; installation preserved")
    for name, image in (("postgres", "postgres:16"), ("mattermost", "mattermost/mattermost-team-edition:12.0.1")):
        record = services[name]
        if record["Config"]["Image"] != image or record["Config"]["Labels"].get("com.docker.compose.project") != project:
            raise ValueError("Legacy image/project identity differs; installation preserved")
    database_env = services["postgres"]["Config"]["Env"]
    if "POSTGRES_PASSWORD=" + creds["database_password"] not in database_env:
        raise ValueError("Legacy database credential differs from the existing container; installation preserved")
    ports = services["mattermost"].get("HostConfig", {}).get("PortBindings", {}).get("8065/tcp") or []
    if not any(item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == str(port) for item in ports):
        raise ValueError("Legacy loopback endpoint differs; installation preserved")
    return creds


def request(path, method="GET", data=None, token=None, content_type=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        content_type = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(BASE + path, data=data, method=method, headers=headers)
    with urlopen(req, timeout=60) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def main(argv=None):
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--state", type=Path, help="Persistent production home; default ~/.local/state/murmur-server")
    parser.add_argument("--project", help="Explicit permanent Compose project; retained in the installation marker")
    parser.add_argument("--port", type=int)
    parser.add_argument("--site-url", type=origin)
    parser.add_argument("--compose-override", type=Path, help="Explicit owned override; never auto-load checkout overrides")
    parser.add_argument("--adopt-legacy", type=Path, help="Deliberately adopt this existing server/.state after identity checks")
    parser.add_argument("--test-project")
    parser.add_argument("--test-port", type=int)
    parser.add_argument("--test-state", type=Path)
    parser.add_argument("--test-site-url", type=origin,
                        help="HTTPS origin or http://murmur-preview.localhost at the bound test port")
    args = parser.parse_args(argv)
    isolated = any(value is not None for value in (args.test_project, args.test_port, args.test_state, args.test_site_url))
    if isolated and any(value is not None for value in (args.state, args.project, args.port, args.site_url,
                                                       args.compose_override, args.adopt_legacy)):
        parser.error("Test and permanent installation options cannot be mixed")
    if isolated:
        if not (args.test_project and args.test_project.startswith("murmur-test-")
                and all(c.islower() or c.isdigit() or c == "-" for c in args.test_project)
                and args.test_port is not None and 1024 <= args.test_port <= 65535
                and args.test_port != 8065 and args.test_state is not None):
            parser.error("Tests require a murmur-test-* project, --test-port (1024–65535, not 8065) and --test-state")
        state = args.test_state.expanduser().resolve()
        install_state = (HERE / ".state").resolve()
        if state == HERE.resolve() or state == install_state or install_state in state.parents:
            parser.error("Isolated tests must use a separate empty state directory")
        project, port = args.test_project, args.test_port
        try:
            site = fixture_origin(args.test_site_url or f"http://127.0.0.1:{port}", port)
        except ValueError as error:
            parser.error(str(error))
        marker = state / "test-installation.json"
    else:
        state = (args.state or Path("~/.local/state/murmur-server")).expanduser().resolve()
        if state == (HERE / ".state").resolve() or HERE.resolve() in state.parents:
            parser.error("Permanent state must live outside the checkout; select --adopt-legacy to retain old state")
        marker = state / "installation.json"
        previous = read_private_json(marker) if marker.exists() else {}
        project = args.project or previous.get("project")
        if not project or project.startswith("murmur-test-") or not all(c.islower() or c.isdigit() or c == "-" for c in project):
            parser.error("Select an explicit lowercase permanent --project (not murmur-test-*)")
        port = args.port if args.port is not None else previous.get("port", 8065)
        if not 1024 <= port <= 65535:
            parser.error("--port must be 1024–65535")
        site = origin(args.site_url or previous.get("site_url", f"http://127.0.0.1:{port}"))
    BASE = f"http://127.0.0.1:{port}"
    binding = {"project": project, "port": port, "site_url": site}
    if marker.exists():
        if args.adopt_legacy:
            parser.error("This installation is already bound; rerun without --adopt-legacy")
        previous = read_private_json(marker)
        # Old test markers omitted the equivalent loopback site URL.
        existing = {key: previous.get(key, BASE if key == "site_url" else None) for key in binding}
        if existing != binding:
            parser.error("State belongs to a different project/port/origin")
    elif state.exists() and any(state.iterdir()):
        parser.error("Refusing an unmarked nonempty installation home")
    env = compose_environment()
    resources = project_resources(project, env)
    if not marker.exists() and any(resources) and not args.adopt_legacy:
        parser.error("Existing project is unbound; select --adopt-legacy after reviewing its installation")
    creds = legacy_credentials(args.adopt_legacy.expanduser().resolve(), project, port, resources, env) if args.adopt_legacy else None
    legacy_config = None
    if args.adopt_legacy:
        legacy = args.adopt_legacy.expanduser().resolve()
        files = ["-f", str(legacy.parent / "compose.yaml")]
        regular_file(legacy.parent / "compose.yaml")
        override = args.compose_override
        if override is None and (legacy.parent / "compose.override.yaml").exists():
            override = legacy.parent / "compose.override.yaml"
        if override:
            regular_file(override)
            files += ["-f", str(override.resolve())]
        legacy_config = json.loads(subprocess.check_output(
            ["docker", "compose", "--project-name", project, "--env-file", str(legacy.parent / ".env"),
             *files, "config", "--format", "json"], env=env, text=True))
        # Adoption retains the actual canonical origin, LAN ports and CORS.
        if args.site_url is None:
            binding["site_url"] = origin(legacy_config["services"]["mattermost"]["environment"]["MM_SERVICESETTINGS_SITEURL"])
        else:
            legacy_config["services"]["mattermost"]["environment"]["MM_SERVICESETTINGS_SITEURL"] = binding["site_url"]
    if args.adopt_legacy and isolated:
        parser.error("Legacy adoption is only available for a permanent installation")
    private_directory(state)
    if not marker.exists():
        private_json(marker, binding)
    credentials = state / "credentials.json"
    if not credentials.exists() and any(resources) and creds is None:
        parser.error("Existing installation credentials are absent; restore them before continuing")
    if credentials.exists():
        existing = read_private_json(credentials)
        if creds is not None and creds != existing:
            parser.error("Selected permanent and legacy credentials disagree")
        creds = existing
    if creds is None:
        creds = {"admin": "murmur-admin", "password": secrets.token_urlsafe(32),
                 "database_password": secrets.token_hex(24), "coordinator_token": secrets.token_urlsafe(48)}
    private_json(credentials, creds)
    env_file = state / ".env"
    if env_file.exists() or env_file.is_symlink():
        regular_file(env_file, private=True)
    write_private(env_file, ("MURMUR_DB_PASSWORD=" + creds["database_password"] + "\n").encode())
    if not args.no_build:
        build_env = dict(env, MURMUR_GO_CACHE=project + "-go-cache")
        subprocess.run(["sh", str(HERE / "build.sh")], check=True, env=build_env)
    archive = HERE / "dist/murmur-0.1.0.tar.gz"
    artifact = release.check_build(HERE.parent, archive)
    compose = ["docker", "compose", "--project-name", project, "--env-file", str(env_file)]
    compose_file = state / "compose.json"
    if compose_file.exists():
        regular_file(compose_file, private=True)
        resolved = json.loads(compose_file.read_text())
        previous = read_private_json(marker)
        if release.digest(compose_file.read_bytes()) != previous.get("compose_sha256"):
            parser.error("Owned Compose configuration was modified; review it before updating")
        if args.compose_override:
            parser.error("An existing installation already has a bound Compose configuration")
    elif legacy_config is not None:
        resolved = legacy_config
    else:
        files = ["-f", str(HERE / "compose.yaml")]
        override = args.compose_override
        if override:
            regular_file(override)
            files += ["-f", str(override.resolve())]
        resolved = json.loads(subprocess.check_output(compose + files + ["config", "--format", "json"], env=env, text=True))
        service = resolved["services"]["mattermost"]
        if isolated:
            service["ports"] = [{"target": 8065, "published": str(port), "host_ip": "127.0.0.1", "protocol": "tcp"}]
        else:
            if not override:
                for entry in service["ports"]:
                    if entry.get("host_ip") == "127.0.0.1" and str(entry.get("target")) == "8065":
                        entry["published"] = str(port)
            if override and args.site_url is None:
                binding["site_url"] = origin(service["environment"]["MM_SERVICESETTINGS_SITEURL"])
        service["environment"]["MM_SERVICESETTINGS_SITEURL"] = binding["site_url"]
    service = resolved["services"]["mattermost"]
    if not any(entry.get("host_ip") == "127.0.0.1" and str(entry.get("target")) == "8065"
               and str(entry.get("published")) == str(port) and entry.get("protocol", "tcp") == "tcp"
               for entry in service.get("ports", [])):
        parser.error("Selected Compose configuration must retain the exact 127.0.0.1:" + str(port)
                     + ":8065/tcp management binding; retain it in --compose-override")
    if not compose_file.exists():
        private_json(compose_file, resolved)
    binding["compose_sha256"] = release.digest(compose_file.read_bytes())
    private_json(marker, binding)
    compose += ["-f", str(compose_file)]
    subprocess.run(compose + ["up", "-d"], cwd=state, env=env, check=True)
    for _ in range(300):
        try:
            request("/api/v4/system/ping")
            break
        except (OSError, URLError, TimeoutError):
            time.sleep(1)
    else:
        raise SystemExit("Mattermost did not become healthy; inspect docker compose logs")
    try:
        request("/api/v4/users", "POST", {
            "email": "murmur-admin@localhost.invalid", "username": creds["admin"], "password": creds["password"],
        })
    except HTTPError as exc:
        # Existing installation is permitted only when these dedicated
        # credentials still work; the subsequent login verifies that.
        if exc.code not in (400, 403):
            raise
    login = Request(BASE + "/api/v4/users/login", data=json.dumps({
        "login_id": creds["admin"], "password": creds["password"],
    }).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(login, timeout=30) as response:
        admin_token = response.headers["Token"]
        user = json.load(response)
    config = request("/api/v4/config", token=admin_token)
    # Each explicitly enrolled native session has its own bot. Mattermost's
    # default team cap of 50 is too small for the local test/session lifecycle.
    config["TeamSettings"]["MaxUsersPerTeam"] = 1000
    # Mattermost restores masked secrets on save; retain all other plugin keys.
    murmur_config = config["PluginSettings"]["Plugins"].setdefault("murmur", {})
    murmur_config["coordinatortoken"] = creds["coordinator_token"]
    request("/api/v4/config", "PUT", config, admin_token)
    boundary = "murmur-" + secrets.token_hex(16)
    upload = ("--" + boundary + '\r\nContent-Disposition: form-data; name="force"\r\n\r\ntrue\r\n'
              "--" + boundary + '\r\nContent-Disposition: form-data; name="plugin"; filename="murmur.tar.gz"\r\n'
              "Content-Type: application/gzip\r\n\r\n").encode() + archive.read_bytes() + ("\r\n--" + boundary + "--\r\n").encode()
    request("/api/v4/plugins", "POST", upload, admin_token, "multipart/form-data; boundary=" + boundary)
    request("/api/v4/plugins/murmur/enable", "POST", {}, admin_token)
    health = request("/plugins/murmur/v1/health", token=creds["coordinator_token"])
    installed = health.get("build", {})
    if (installed.get("source_sha256") != artifact["source_sha256"]
            or installed.get("compatibility") != artifact["compatibility"]):
        raise SystemExit("Installed plugin identity/compatibility does not match the selected build")
    private_json(state / "coordinator.json", {
        "server_url": binding["site_url"] + "/plugins/murmur/v1", "token": creds["coordinator_token"],
    })
    private_json(state / "admin-session.json", {"token": admin_token, "user_id": user["id"]})
    binding["build"] = {key: artifact[key] for key in ("revision", "source_sha256", "compatibility", "archive_sha256")}
    private_json(marker, binding)
    print("Mattermost is running at " + (site if isolated else BASE))
    print("Coordinator configuration: " + str(state / "coordinator.json"))
    print("Local operator credentials: " + str(credentials))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
