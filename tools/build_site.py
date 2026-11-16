#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Gather the docs into site-src/ for MkDocs (mkdocs.yml builds from there).

The README and CHANGELOG live at the top of the repository and the guides in
docs/, and their links are written for GitHub. This copies them into one
folder and fixes the links so they work on the site:

  * README.md becomes index.md; its docs/X.md links become X.md
  * links between pages stay as they are (MkDocs turns X.md into X/)
  * images in docs/images/ are copied to images/ on the site
  * links to anything else in the repository (the example config, scripts,
    LICENSE) point at the file on GitHub instead

Run it from anywhere; it always works on the repository it's in.
    python3 tools/build_site.py && mkdocs build --strict
"""
from __future__ import annotations

import posixpath
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site-src"
GITHUB = "https://github.com/rtfmoz2/star-lan-bridge/blob/main/"

# repository path -> page name on the site
PAGES = {"README.md": "index.md", "CHANGELOG.md": "CHANGELOG.md"}
PAGES.update({f"docs/{p.name}": p.name for p in sorted((ROOT / "docs").glob("*.md"))})

LINK = re.compile(r"\]\(([^)\s]+)\)")


def fix_link(target: str, source: str) -> str:
    if re.match(r"[a-z]+:|#|/", target):  # web link, anchor on the same page, or absolute
        return target
    path, hashpart = (target.split("#", 1) + [""])[:2]
    repo_path = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    anchor = f"#{hashpart}" if hashpart else ""
    if repo_path in PAGES:
        return PAGES[repo_path] + anchor
    if repo_path.startswith("docs/images/"):
        return repo_path[len("docs/"):]
    return GITHUB + repo_path + anchor


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    if (ROOT / "docs" / "images").is_dir():
        shutil.copytree(ROOT / "docs" / "images", OUT / "images")
    for source, page in PAGES.items():
        text = (ROOT / source).read_text(encoding="utf-8")
        text = LINK.sub(lambda m: f"]({fix_link(m.group(1), source)})", text)
        (OUT / page).write_text(text, encoding="utf-8")
    print(f"{len(PAGES)} pages in {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
