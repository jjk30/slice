"""Repository hygiene checks that keep house style from drifting back in."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)
# The spellings that render as the glyph without carrying the character itself: the
# Python/JS escape and the three HTML entities. Built by concatenation so this file does
# not trip its own check.
EM_DASH_SPELLINGS = (
    EM_DASH,
    "\\" + "u2014",
    "&" + "mdash;",
    "&#" + "8212;",
    "&#" + "x2014;",
)


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [ROOT / name for name in out.split("\0") if name]


def em_dash_offenders(paths, root: Path = ROOT) -> list[str]:
    """``path:line`` for every line of a text file that spells an em dash in any way."""
    offenders: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a picture or other binary, not text
        for number, line in enumerate(text.splitlines(), 1):
            if any(spelling in line for spelling in EM_DASH_SPELLINGS):
                offenders.append(f"{path.relative_to(root)}:{number}")
    return offenders


def test_no_em_dash_in_any_tracked_text_file():
    """No tracked text file may carry an em dash: not the character (U+2014), not the
    ``\\u`` escape, not an HTML entity for it. Comments, docs and UI placeholders use a
    comma, a colon, a split sentence, or a plain hyphen instead; code and tests that must
    name the glyph itself spell it as ``chr(0x2014)``.
    """
    offenders = em_dash_offenders(_tracked_files())
    assert not offenders, "em dash found in:\n" + "\n".join(offenders)


def test_em_dash_check_catches_every_spelling(tmp_path):
    """Each spelling is caught on its own; a clean file and a binary file are not."""
    bad = tmp_path / "bad.txt"
    for spelling in EM_DASH_SPELLINGS:
        bad.write_text(f"a {spelling} b", encoding="utf-8")
        assert em_dash_offenders([bad], root=tmp_path) == ["bad.txt:1"], repr(spelling)
    bad.write_text("a - b, a: b", encoding="utf-8")
    assert em_dash_offenders([bad], root=tmp_path) == []
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\xe2\x80\x94")
    assert em_dash_offenders([tmp_path / "pic.png"], root=tmp_path) == []
