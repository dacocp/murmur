#!/usr/bin/env python3
"""Package plain JavaScript against Mattermost's host React; no npm runtime."""
from pathlib import Path
import shutil

here = Path(__file__).resolve().parent
dist = here.parent / "server/dist"
(dist / "webapp").mkdir(parents=True, exist_ok=True)
(dist / "public").mkdir(parents=True, exist_ok=True)
model = (here / "model.js").read_text()
app = (here / "index.js").read_text()
(dist / "webapp/index.js").write_text(
    "(()=>{\nconst Model=(()=>{const module={exports:{}};\n" + model +
    "\nreturn module.exports;})();\n" + app + "\n})();\n")
shutil.copyfile(here / "console.css", dist / "public/console.css")
