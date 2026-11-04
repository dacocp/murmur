#!/usr/bin/env python3
"""Qualify the companion only on an explicitly marked disposable MM fixture.

First use server/bootstrap.py --test-project ... --test-port ... --test-state ...
with fresh state. This harness installs/disables/re-enables only murmur-preview,
creates an ordinary synthetic human, and uses real cookie authentication. It
never discovers a live server, copies live data or enrolls a native agent.
Browser login-return and BFCache/resume privacy need separate browser evidence.
"""

import argparse
from http.cookiejar import CookieJar
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from ops_files import private_json, read_private_json
from server.fixture import fixture_arguments, load_fixture


APP = "/plugins/murmur-preview/public/app/app.html"
OPERATOR = "/plugins/murmur/operator/v1"
ASSETS = {"app.html": "text/html", "app.js": "javascript", "api.js": "javascript", "app.css": "text/css",
          "compose.js": "javascript", "operator-model.js": "javascript"}


def proof_origin(fixture):
    # The dedicated local demo host is also its browser-auth origin. Preserve
    # the existing loopback transport for unrelated HTTPS/proxy fixtures.
    return fixture.origin if fixture.origin.startswith("http://murmur-preview.localhost:") else fixture.management_origin


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csp = None
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy":
            self.csp = attrs.get("content")
        if tag == "script" and attrs.get("src"):
            self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet" and attrs.get("href"):
            self.assets.append(attrs["href"])


class Client:
    def __init__(self, origin, token=None):
        self.origin, self.token = origin, token
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.user_id = None

    def call(self, path, data=None, *, method=None, expected=200, headers=None):
        values = {"X-Requested-With": "XMLHttpRequest"}
        if self.token:
            values["Authorization"] = "Bearer " + self.token
        if self.user_id and path.startswith(OPERATOR):
            values["X-Murmur-User-Id"] = self.user_id
        for cookie in self.cookies:
            if cookie.name == "MMCSRF":
                values["X-CSRF-Token"] = cookie.value
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        request = Request(self.origin + path, data=data, method=method, headers=values)
        try:
            response = self.opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            allowed = expected if isinstance(expected, tuple) else (expected,)
            if response.status not in allowed:
                raise AssertionError(f"{request.get_method()} {path}: expected {allowed}, got {response.status}")
            return response.read(), response.headers, response.geturl()

    def json(self, path, data=None, **options):
        body, headers, _ = self.call(path, data, **options)
        return (json.loads(body) if body else None), headers

    def login(self, username, password):
        user, _ = self.json("/api/v4/users/login", {"login_id": username, "password": password})
        self.user_id = user["id"]
        if not any(cookie.name == "MMAUTHTOKEN" for cookie in self.cookies):
            raise AssertionError("Login did not establish an HTTP authentication cookie")


def package_contents(path):
    with tarfile.open(path, "r:gz") as archive:
        expected = {"plugin.json", "webapp/index.js", "build.json", *("public/app/" + name for name in ASSETS)}
        entries = archive.getmembers()
        if {entry.name for entry in entries} != expected or not all(entry.isfile() for entry in entries):
            raise ValueError("Expected only companion manifest/launcher/build metadata and allowlisted public assets")
        files = {entry.name: archive.extractfile(entry).read() for entry in entries}
    manifest = json.loads(files["plugin.json"])
    if manifest.get("id") != "murmur-preview" or "server" in manifest:
        raise ValueError("Only the serverless murmur-preview companion may be installed by this harness")
    build = json.loads(files["build.json"])
    for name, content in files.items():
        source = "launcher.js" if name == "webapp/index.js" else name.removeprefix("public/app/")
        if name != "build.json" and hashlib.sha256(content).hexdigest() != build["inputs"].get(source):
            raise ValueError("Companion content does not match its embedded input hash: " + name)
    return files


def install(fixture, archive):
    """Install only the allowlisted companion in the already selected fixture."""
    files = package_contents(archive)
    admin = Client(proof_origin(fixture), fixture.admin["token"])
    boundary = "murmur-preview-" + secrets.token_hex(16)
    upload = ("--" + boundary + '\r\nContent-Disposition: form-data; name="force"\r\n\r\ntrue\r\n'
              "--" + boundary + '\r\nContent-Disposition: form-data; name="plugin"; filename="preview.tar.gz"\r\n'
              "Content-Type: application/gzip\r\n\r\n").encode() + archive.read_bytes() + ("\r\n--" + boundary + "--\r\n").encode()
    admin.call("/api/v4/plugins", upload, method="POST", expected=201,
               headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    admin.call("/api/v4/plugins/murmur-preview/enable", {}, method="POST")
    return files, admin


def verify_assets(fixture, files):
    anonymous = Client(proof_origin(fixture))
    observations = {}
    for name, mime in ASSETS.items():
        body, headers, final = anonymous.call(APP.rsplit("/", 1)[0] + "/" + name)
        assert body == files["public/app/" + name], "Served asset differs from reviewed package: " + name
        assert mime in headers.get("Content-Type", ""), "Incorrect asset MIME: " + name
        assert final == proof_origin(fixture) + APP.rsplit("/", 1)[0] + "/" + name, "Unexpected asset redirect"
        observations[name] = {key: headers.get(key) for key in (
            "Content-Type", "Cache-Control", "Content-Security-Policy", "ETag", "Last-Modified")}
    page = Page()
    page.feed(files["public/app/app.html"].decode())
    assert page.csp, "Standalone HTML needs an explicit meta CSP"
    referenced = set()
    for reference in page.assets:
        parsed = urlsplit(reference)
        name = parsed.path
        assert not parsed.scheme and not parsed.netloc and not parsed.fragment and name in ASSETS and name != "app.html", "Unapproved public asset reference"
        expected_hash = hashlib.sha256(files["public/app/" + name]).hexdigest()
        assert parse_qs(parsed.query) == {"v": [expected_hash]}, "Asset URL lacks its exact emitted content hash: " + name
        url = APP.rsplit("/", 1)[0] + "/" + reference
        body, headers, final = anonymous.call(url)
        assert body == files["public/app/" + name] and ASSETS[name] in headers.get("Content-Type", ""), "Versioned asset bytes/MIME mismatch: " + name
        assert final == proof_origin(fixture) + url, "Versioned asset redirected"
        observations[name]["verified_cache_busted_url"] = final
        referenced.add(name)
    assert referenced == set(ASSETS) - {"app.html"}, "HTML must reference every allowlisted runtime asset through its content hash"
    anonymous.call(APP.rsplit("/", 1)[0] + "/", expected=404)
    return observations


def exercise(fixture, archive):
    version = subprocess.check_output(
        fixture.compose + ["exec", "-T", "mattermost", "bin/mattermost", "version"],
        cwd=fixture.state, text=True, stderr=subprocess.DEVNULL)
    if "Version: 12.0.1" not in version:
        raise AssertionError("The disposable runtime is not the pinned Mattermost 12.0.1")
    credentials = read_private_json(fixture.state / "credentials.json")
    anonymous = Client(proof_origin(fixture))
    cookie = Client(proof_origin(fixture))
    ordinary = Client(proof_origin(fixture))
    files, admin = install(fixture, archive)
    checks = []
    disabled = False
    try:
        observations = verify_assets(fixture, files)
        checks.append("exact public assets, MIME, meta CSP and non-directory app.html entry")

        anonymous.call(OPERATOR + "/bootstrap", expected=401,
                       headers={"Mattermost-User-Id": fixture.admin["user_id"]})
        cookie.login(credentials["admin"], credentials["password"])
        boot, bootstrap_headers = cookie.json(OPERATOR + "/bootstrap")
        assert boot["user"]["id"] == cookie.user_id == fixture.admin["user_id"], "Cookie identity was not bound"
        cookie.call(OPERATOR + "/bootstrap", expected=409, headers={"X-Murmur-User-Id": "different-human"})
        checks.append("anonymous 401, real cookie-authenticated admin bootstrap and mismatched-human 409")

        username, password = "murmur-preview-" + secrets.token_hex(5), "Preview-" + secrets.token_urlsafe(24) + "aA1!"
        user, _ = admin.json("/api/v4/users", {"username": username, "email": username + "@example.invalid",
                                               "password": password}, expected=201)
        assert "system_admin" not in user["roles"].split(), "Expected an ordinary fixture human"
        private_json(fixture.state / "preview-user.json", {"username": username, "password": password,
                                                            "user_id": user["id"]})
        ordinary.login(username, password)
        ordinary.call(OPERATOR + "/bootstrap", expected=403)
        ordinary.call(OPERATOR + "/bootstrap", expected=403,
                      headers={"Mattermost-User-Id": cookie.user_id, "X-Murmur-User-Id": cookie.user_id})
        checks.append("ordinary cookie user 403, including spoofed upstream identity")

        # HTTP route + cookie evidence is distinct from a browser following the login flow.
        anonymous.call("/login?redirect_to=" + quote(APP, safe=""))
        checks.append("native login-return route serves; cookie session accesses standalone app and operator API")
        cache = bootstrap_headers.get("Cache-Control", "")
        assert "no-store" in cache.lower() and "private" in cache.lower(), "Authenticated bootstrap lacks private,no-store"
        checks.append("authenticated bootstrap Cache-Control private,no-store")

        admin.call("/api/v4/plugins/murmur-preview/disable", {}, method="POST")
        disabled = True
        for name in ASSETS:
            anonymous.call(APP.rsplit("/", 1)[0] + "/" + name, expected=404)
        cookie.call(OPERATOR + "/bootstrap")
        checks.append("companion disable denies uncached asset requests while main Murmur API remains available")
        admin.call("/api/v4/plugins/murmur-preview/enable", {}, method="POST")
        disabled = False
        anonymous.call(APP)
        checks.append("companion re-enable restores app.html")
        return {"mattermost_version": "12.0.1", "origin": fixture.origin,
                "request_origin": proof_origin(fixture), "management_origin": fixture.management_origin,
                "project": fixture.project, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "checks": checks, "static_response_headers": observations,
                "bootstrap_cache_control": cache,
                "remaining_browser_checks": ["actual login form and redirect return", "launcher registration and disable cleanup",
                                             "logout/expiry/user switch then Back/BFCache/pageshow/resume concealment"],
                "limits": ["No physical device, HTTPS/proxy or deployment proof", "Disable cannot remotely erase an already loaded document or HTTP cache"]}
    finally:
        if disabled:
            admin.call("/api/v4/plugins/murmur-preview/enable", {}, method="POST")
        for client in (ordinary, cookie):
            if client.user_id:
                client.call("/api/v4/users/logout", {}, method="POST")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    fixture_arguments(parser)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--install-only", action="store_true",
                        help="Update companion and verify public assets only; do not create users, log in or toggle disable")
    args = parser.parse_args()
    try:
        fixture = load_fixture(args.test_state, args.test_project, args.test_origin, require_admin=True)
        archive = args.archive.resolve()
        if args.install_only:
            files, _ = install(fixture, archive)
            result = {"mode": "install-only", "origin": fixture.origin, "project": fixture.project,
                      "request_origin": proof_origin(fixture), "management_origin": fixture.management_origin,
                      "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                      "static_response_headers": verify_assets(fixture, files),
                      "checks": ["updated companion; exact public assets, MIME, meta CSP and non-directory app.html entry"],
                      "limits": ["Authentication, disable and browser checks are not rerun in install-only mode"]}
        else:
            result = exercise(fixture, archive)
        report = fixture.state / ("preview-install-result.json" if args.install_only else "preview-hosting-result.json")
        private_json(report, result)
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"report": str(report), "archive_sha256": result["archive_sha256"],
                      "checks_passed": len(result["checks"])}, sort_keys=True))


if __name__ == "__main__":
    main()
