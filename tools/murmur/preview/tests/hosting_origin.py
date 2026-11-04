#!/usr/bin/env python3
"""Give a marked reader fixture a separate browser cookie hostname.

Dry-run by default. --apply updates only canonical-origin metadata and recreates
the selected test Mattermost service with its existing loopback port/volumes.
It never reads browser cookies, logs in, revokes sessions or installs plugins.
"""

import argparse
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from ops_files import private_json, read_private_json, write_private
from server.fixture import fixture_arguments, fixture_origin, load_fixture


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def origin_plan(fixture):
    port = urlsplit(fixture.management_origin).port
    target = fixture_origin(f"http://murmur-preview.localhost:{port}", port)
    if fixture.origin not in {fixture.management_origin, target}:
        raise ValueError("Only the selected loopback reader fixture can adopt this demo hostname")
    state = fixture.state
    marker = read_private_json(state / "test-installation.json")
    compose = read_private_json(state / "compose.json")
    if marker.get("compose_sha256") != hashlib.sha256((state / "compose.json").read_bytes()).hexdigest():
        raise ValueError("Fixture Compose content differs from its marker; preserve it for review")
    service = compose["services"]["mattermost"]
    if service.get("ports") != [{"target": 8065, "published": str(port), "host_ip": "127.0.0.1", "protocol": "tcp"}]:
        raise ValueError("Expected the fixture's single existing loopback binding")
    environment = service["environment"]
    if environment.get("MM_SERVICESETTINGS_SITEURL") != fixture.origin:
        raise ValueError("Fixture canonical origin and saved Compose environment disagree")
    if str(environment.get("MM_SERVICESETTINGS_ALLOWCOOKIESFORSUBDOMAINS", "false")).lower() != "false":
        raise ValueError("Demo isolation requires the existing host-only cookie policy")
    environment["MM_SERVICESETTINGS_SITEURL"] = target
    updates = {state / "compose.json": encoded(compose)}
    marker["site_url"] = target
    marker["compose_sha256"] = hashlib.sha256(updates[state / "compose.json"]).hexdigest()
    updates[state / "test-installation.json"] = encoded(marker)
    coordinator = read_private_json(state / "coordinator.json")
    field = "server_url" if "server_url" in coordinator else "url"
    coordinator[field] = target + "/plugins/murmur/v1"
    updates[state / "coordinator.json"] = encoded(coordinator)
    for name in ("reader-seed-plan.json", "reader-seed.json"):
        path = state / name
        if path.exists():
            value = read_private_json(path)
            if value.get("origin") != fixture.origin:
                raise ValueError("Seed origin differs from its fixture; preserve existing identities/history")
            value["origin"] = target
            updates[path] = encoded(value)
    return target, updates


def checked_runtime(fixture):
    container = subprocess.check_output(fixture.compose + ["ps", "-q", "mattermost"], text=True).strip()
    if not container or "\n" in container:
        raise ValueError("Expected one running fixture Mattermost container")
    record = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
    labels = record["Config"]["Labels"]
    if labels.get("com.docker.compose.project") != fixture.project or labels.get("com.docker.compose.service") != "mattermost":
        raise ValueError("Container identity differs from the selected test fixture")
    raw = subprocess.check_output(["docker", "cp", container + ":/mattermost/config/config.json", "-"],
                                  stderr=subprocess.DEVNULL)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        configuration = json.load(archive.extractfile(archive.getmembers()[0]))
    if configuration["ServiceSettings"].get("AllowCookiesForSubdomains") is not False:
        raise ValueError("Running fixture does not have the required existing host-only cookie policy")
    return container


def verify_origin(target):
    with urlopen(target + "/api/v4/config/client?format=old", timeout=5) as response:
        configuration = json.load(response)
    if configuration.get("SiteURL") != target:
        raise ValueError("Running fixture has not adopted its selected canonical hostname")
    with urlopen(target + "/plugins/murmur-preview/public/app/app.html", timeout=5) as response:
        if response.status != 200 or response.geturl() != target + "/plugins/murmur-preview/public/app/app.html":
            raise ValueError("Demo app does not resolve directly on its selected hostname")


def apply_origin(fixture, target, updates):
    checked_runtime(fixture)
    addresses = {info[4][0] for info in socket.getaddrinfo("murmur-preview.localhost", None, type=socket.SOCK_STREAM)}
    if not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError("Demo hostname must resolve only to loopback addresses")
    original = {path: path.read_bytes() for path in updates}
    changed = any(content != original[path] for path, content in updates.items())
    command = fixture.compose + ["up", "-d", "--no-deps", "--force-recreate", "mattermost"]
    if changed:
        try:
            for path, content in updates.items():
                write_private(path, content)
            subprocess.run(command, check=True, capture_output=True, timeout=120)
            deadline = time.monotonic() + 90
            while True:
                try:
                    verify_origin(target)
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise ValueError("Timed out waiting for the demo canonical origin")
                    time.sleep(1)
        except (OSError, ValueError, subprocess.SubprocessError):
            for path, content in original.items():
                write_private(path, content)
            subprocess.run(command, check=True, capture_output=True, timeout=120)
            raise ValueError("Demo rebind failed; original fixture metadata/service configuration was restored") from None
    else:
        verify_origin(target)
    private_json(fixture.state / "preview-demo-origin.json", {
        "project": fixture.project, "origin": target, "management_origin": fixture.management_origin,
        "app_url": target + "/plugins/murmur-preview/public/app/app.html",
        "host_only_cookie_policy": True, "restarted": changed,
        "preserved": ["loopback binding", "volumes", "credentials", "admin session", "seed identities and posts"],
        "limits": ["Old same-host browser cookies were not inspected or repaired", "Use the fixture account, not live credentials"],
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed fixture-only origin plan and controlled restart")
    args = parser.parse_args()
    try:
        fixture = load_fixture(args.test_state, args.test_project, args.test_origin)
        target, updates = origin_plan(fixture)
        if args.apply:
            apply_origin(fixture, target, updates)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"applied": args.apply, "project": fixture.project,
                      "demo_url": target + "/plugins/murmur-preview/public/app/app.html"}, sort_keys=True))


if __name__ == "__main__":
    main()
