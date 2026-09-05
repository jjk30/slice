"""Repository hygiene checks that keep house style from drifting back in."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM_DASH = "\u2014"
CHECKED_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".html", ".js"}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [ROOT / name for name in out.split("\0") if name]


def test_no_em_dash_in_tracked_source_and_docs():
    """No tracked .py, .md, .yml/.yaml, .html or .js file may contain an em dash (U+2014).

    Comments, docstrings and docs use a comma, a colon, or a split sentence instead; the
    few places that must emit the glyph itself (a dashboard placeholder, a test that
    asserts a reply carries none) spell it as an escape, so the character never appears
    in source.
    """
    offenders: list[str] = []
    for path in _tracked_files():
        if path.suffix not in CHECKED_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if EM_DASH in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, "em dash (U+2014) found in:\n" + "\n".join(offenders)
