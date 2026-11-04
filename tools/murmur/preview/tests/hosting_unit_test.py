"""Small packaging/launcher checks; HTTP acceptance lives in hosting.py."""

import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preview_build", HERE / "build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AssetReferences(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and "src" in attrs:
            self.urls.append(attrs["src"])
        elif tag == "link" and attrs.get("rel") == "stylesheet":
            self.urls.append(attrs["href"])


class CompanionTests(unittest.TestCase):
    def sources(self, directory):
        source = Path(directory) / "source"
        source.mkdir()
        for name in ("build.py", "plugin.json", "launcher.js"):
            shutil.copyfile(HERE / name, source / name)
        model = source.parent / "webapp"
        model.mkdir()
        shutil.copyfile(HERE.parent / "webapp" / "model.js", model / "model.js")
        for name, text in {"app.html": '<link rel="stylesheet" href="app.css">' + ''.join(
                               f'<script src="{name}"></script>' for name in ("api.js", "operator-model.js", "compose.js", "app.js")),
                           "app.js": 'import "./api.js";\n', "api.js": 'export const readonly = true;\n',
                           "app.css": 'body { color: black; }\n', "compose.js": 'window.MurmurCompose = {};\n'}.items():
            (source / name).write_text(text)
        return source

    def test_reproducible_exact_bundle_and_changed_asset_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.sources(directory)
            (source / "credentials.json").write_text('"must not ship"')
            archive, first = builder.package(source)
            original = archive.read_bytes()
            _, again = builder.package(source)
            self.assertEqual(original, archive.read_bytes())
            self.assertEqual(first, again)
            with tarfile.open(archive) as bundle:
                self.assertEqual(set(bundle.getnames()), {
                    "plugin.json", "webapp/index.js", "build.json", "public/app/app.html",
                    "public/app/app.js", "public/app/api.js", "public/app/app.css",
                    "public/app/compose.js", "public/app/operator-model.js"})
                self.assertTrue(all(entry.isfile() and entry.mtime == 0 for entry in bundle.getmembers()))
                self.assertEqual(bundle.extractfile("public/app/app.js").read(), (source / "app.js").read_bytes())
                generated = bundle.extractfile("public/app/operator-model.js").read()
                self.assertIn((source.parent / "webapp" / "model.js").read_bytes(), generated)
                self.assertEqual(first["inputs"]["../webapp/model.js"], builder.digest((source.parent / "webapp" / "model.js").read_bytes()))
                html = bundle.extractfile("public/app/app.html").read()
                self.assertNotEqual(html, (source / "app.html").read_bytes())
                self.assertEqual(first["inputs"]["source/app.html"], builder.digest((source / "app.html").read_bytes()))
                self.assertEqual(first["inputs"]["app.html"], builder.digest(html))
                refs = AssetReferences(html.decode()).urls
                self.assertEqual(len(refs), 5)
                first_urls = {urlsplit(ref).path: ref for ref in refs}
                self.assertEqual(set(first_urls), {"app.css", "api.js", "operator-model.js", "compose.js", "app.js"})
                for ref in refs:
                    parsed = urlsplit(ref)
                    self.assertEqual(parse_qs(parsed.query), {"v": [builder.digest(bundle.extractfile("public/app/" + parsed.path).read())]})
            (source / "app.js").write_text('import "./api.js"; console.log("changed");\n')
            _, changed = builder.package(source)
            self.assertNotEqual(first["source_sha256"], changed["source_sha256"])
            self.assertNotEqual(first["archive_sha256"], changed["archive_sha256"])
            with tarfile.open(archive) as bundle:
                new_urls = {urlsplit(ref).path: ref for ref in AssetReferences(bundle.extractfile("public/app/app.html").read().decode()).urls}
                self.assertNotEqual(first_urls["app.js"], new_urls["app.js"])
                self.assertEqual(parse_qs(urlsplit(new_urls["app.js"]).query), {"v": [builder.digest((source / "app.js").read_bytes())]})
                self.assertEqual({key: url for key, url in first_urls.items() if key != "app.js"},
                                 {key: url for key, url in new_urls.items() if key != "app.js"})
                self.assertEqual(first["inputs"]["source/app.html"], changed["inputs"]["source/app.html"])
                self.assertNotEqual(first["inputs"]["app.html"], changed["inputs"]["app.html"])

    def test_document_cannot_silently_omit_required_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.sources(directory)
            (source / "app.html").write_text('<script src="app.js"></script>')
            with self.assertRaisesRegex(ValueError, "reference every required asset"):
                builder.package(source)
            self.assertFalse((source / "dist").exists())

    def test_missing_asset_or_server_manifest_never_builds_partial_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.sources(directory)
            (source / "api.js").unlink()
            with self.assertRaisesRegex(ValueError, "api.js"):
                builder.package(source)
            self.assertFalse((source / "dist").exists())
            (source / "api.js").write_text("export {};\n")
            manifest = json.loads((source / "plugin.json").read_text())
            manifest["server"] = {"executables": {"linux-amd64": "server/plugin"}}
            (source / "plugin.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "serverless"):
                builder.package(source)
            self.assertFalse((source / "dist").exists())

    def test_launcher_registers_one_inert_entry_then_navigates_on_click(self):
        subprocess.run(["node", "-e", r"""
            const assert = require('node:assert/strict');
            const fs = require('node:fs'), vm = require('node:vm');
            const actions = [], navigations = []; let plugin;
            const context = {window: {
                registerPlugin(id, value) { assert.equal(id, 'murmur-preview'); plugin = value; },
                location: {assign(path) { navigations.push(path); }},
            }, fetch() { throw Error('Launcher must not request data'); }};
            vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context, {timeout: 1000});
            assert(plugin); assert.equal(actions.length, 0); assert.equal(navigations.length, 0);
            plugin.initialize({registerMainMenuAction(label, action) {actions.push({label, action});}});
            assert.equal(actions.length, 1); assert.equal(navigations.length, 0);
            actions[0].action();
            assert.deepEqual(navigations, ['/plugins/murmur-preview/public/app/app.html']);
        """, str(HERE / "launcher.js")], check=True)


if __name__ == "__main__":
    unittest.main()
