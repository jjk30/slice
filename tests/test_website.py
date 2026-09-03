"""Guards on the static marketing site under website/ (served as-is by Caddy).

The how-to page is hand-written HTML with real commands and URLs in it, so the checks
are about presence and hygiene: the file exists, every section the page promises is
there, it never carries a real credential (the blocks show placeholders, never a key),
and it keeps to the site's plain style (no em dashes anywhere). The two headers that
link to it are checked too, so the page can't quietly go unreachable.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
HOW_TO = WEBSITE / "how-to.html"
INDEX = WEBSITE / "index.html"
DASHBOARD_APP = ROOT / "dashboard" / "src" / "App.vue"
PYPROJECT = ROOT / "pyproject.toml"

# The eight sections, in page order. Each must be an <h2> on the page.
HEADINGS = [
    "Install",
    "Log in",
    "Point your tools at slice",
    "Watch spend",
    "Connect AWS",
    "GitHub Actions",
    "Alert emails",
    "Uninstall and revoke",
]

# Shapes of the credentials this project handles. A placeholder like ``slk_live_...`` or
# ``(your slice key)`` never matches; a real value would.
SECRET_PATTERNS = {
    "slice key": r"slk_live_[A-Za-z0-9_-]{8,}",
    "anthropic key": r"sk-ant-[A-Za-z0-9_-]{8,}",
    "openai key": r"sk-proj-[A-Za-z0-9_-]{8,}",
    "aws access key id": r"AKIA[0-9A-Z]{16}",
    "github token": r"gh[pousr]_[A-Za-z0-9]{20,}",
    "resend key": r"\bre_[A-Za-z0-9]{20,}",
    "jwt": r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


@pytest.fixture(scope="module")
def how_to() -> str:
    assert HOW_TO.is_file(), f"missing {HOW_TO}"
    return HOW_TO.read_text(encoding="utf-8")


def test_how_to_page_exists_and_is_html(how_to):
    assert how_to.lstrip().lower().startswith("<!doctype html>")
    assert "<title>" in how_to


@pytest.mark.parametrize("heading", HEADINGS)
def test_how_to_page_has_each_section_heading(how_to, heading):
    pattern = rf"<h2[^>]*>(?:\s*<span[^>]*>[^<]*</span>)?\s*{re.escape(heading)}\s*</h2>"
    assert re.search(pattern, how_to), f"no <h2> for {heading!r}"


def test_how_to_sections_are_in_order(how_to):
    positions = [how_to.index(f">{h}</h2>") for h in HEADINGS]
    assert positions == sorted(positions)


@pytest.mark.parametrize("name,pattern", sorted(SECRET_PATTERNS.items()))
def test_how_to_page_carries_no_secret(how_to, name, pattern):
    hit = re.search(pattern, how_to)
    assert hit is None, f"{name} shaped string on the page: {hit.group(0)[:12]}..."


def test_how_to_page_has_no_em_dash(how_to):
    assert "\u2014" not in how_to


def test_how_to_page_has_copy_buttons_and_one_script(how_to):
    assert how_to.count('class="copy"') >= 8
    assert how_to.count("<script>") == 1


def test_how_to_page_shows_the_current_cli_commands(how_to):
    """The install and login blocks match the CLI as it ships (0.2.1: hosted default)."""
    version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert "pip install slice-gateway" in how_to
    assert "slice --version" in how_to
    assert f"slice-gateway {version}" in how_to
    assert "pip show" not in how_to
    # Login is the bare command; the hosted gateway is the default, not a flag.
    assert "slice login" in how_to
    assert "--base-url https://api.sliceapp.dev" not in how_to
    # Self-hosted users get the one line that points the CLI at their own box.
    assert "--base-url http://localhost:8080" in how_to
    assert "SLICE_BASE_URL" in how_to


def test_site_header_links_to_how_to():
    assert 'href="how-to.html"' in INDEX.read_text(encoding="utf-8")


def test_dashboard_header_links_to_how_to():
    app = DASHBOARD_APP.read_text(encoding="utf-8")
    assert 'href="https://sliceapp.dev/how-to.html"' in app
    assert 'target="_blank"' in app
