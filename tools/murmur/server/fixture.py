"""Explicit marked-server selection for every mutating acceptance harness.

Importing this module reads no settings. Selecting a protected marker supplies
the project/origin binding; optional overrides must agree, never retarget it.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from ops_files import read_private_json, regular_file


@dataclass(frozen=True)
class Fixture:
    state: Path
    project: str
    origin: str
    management_origin: str
    coordinator: dict
    admin: dict | None
    compose: list


def fixture_origin(value, port):
    """Preserve legacy fixtures; give the reader demo a separate cookie host."""
    if not isinstance(value, str):
        raise ValueError("Invalid fixture origin")
    loopback = f"http://127.0.0.1:{port}"
    demo = f"http://murmur-preview.localhost:{port}"
    try:
        parsed = urlsplit(value)
        valid = (not parsed.username and not parsed.password and parsed.path == ""
                 and not parsed.query and not parsed.fragment
                 and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
                 and (value in {loopback, demo} or parsed.scheme == "https" and parsed.hostname
                      and (parsed.port is None or 1 <= parsed.port <= 65535)))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Fixture origin must be the bound loopback, exact murmur-preview.localhost test port, or HTTPS")
    return value


def load_fixture(test_state=None, project=None, origin=None, *, require_admin=False):
    selected = test_state or os.environ.get("MURMUR_TEST_STATE")
    if not selected:
        raise ValueError("Select a marked disposable installation with --test-state or MURMUR_TEST_STATE; "
                         "a coordinator config alone never selects a test server")
    state = Path(selected).expanduser().resolve()
    marker = read_private_json(state / "test-installation.json")
    name, port = marker.get("project"), marker.get("port")
    if (not isinstance(name, str) or not re.fullmatch(r"murmur-test-[a-z0-9-]+", name)
            or type(port) is not int or not 1024 <= port <= 65535 or port == 8065):
        raise ValueError("Invalid disposable project/port binding")
    loopback = f"http://127.0.0.1:{port}"
    expected = fixture_origin(marker.get("site_url", loopback), port)
    override_project = project or os.environ.get("MURMUR_TEST_PROJECT")
    override_origin = origin or os.environ.get("MURMUR_TEST_ORIGIN") or os.environ.get("MURMUR_TEST_HTTPS_ORIGIN")
    if override_project and override_project != name or override_origin and override_origin.rstrip("/") != expected:
        raise ValueError("Explicit fixture project/origin disagrees with its marker")
    old_config = os.environ.get("MURMUR_INTEGRATION_CONFIG")
    if old_config and Path(old_config).expanduser().resolve() != state / "coordinator.json":
        raise ValueError("MURMUR_INTEGRATION_CONFIG disagrees with the marked fixture")
    coordinator = read_private_json(state / "coordinator.json")
    if coordinator.get("server_url", coordinator.get("url")) != expected + "/plugins/murmur/v1":
        raise ValueError("Coordinator URL disagrees with the disposable project/origin")
    if not isinstance(coordinator.get("token"), str) or not coordinator["token"]:
        raise ValueError("Fixture coordinator token is absent")
    admin = read_private_json(state / "admin-session.json") if require_admin else None
    return Fixture(state, name, expected, loopback, coordinator, admin,
                   ["docker", "compose", "--project-name", name, "--env-file", str(state / ".env"),
                    "-f", str(state / "compose.json")])


def fixture_arguments(parser):
    parser.add_argument("--test-state", type=Path, help="Marked disposable installation; defaults to MURMUR_TEST_STATE")
    parser.add_argument("--test-project", help="Optional expected project; must match the selected marker")
    parser.add_argument("--test-origin", help="Optional expected origin; must match the selected marker")
