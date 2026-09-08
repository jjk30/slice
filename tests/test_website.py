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
# The email logo (phase 26): served at https://sliceapp.dev/logo.png, shown at 48 CSS px.
LOGO = WEBSITE / "logo.png"
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


def test_logo_png_exists_and_is_192px_wide():
    """The email HTML points at https://sliceapp.dev/logo.png (app/alerts/channels.py); the
    file must exist here, be a real PNG (not JPEG bytes under a .png name), and be 192px
    wide so it stays sharp at 48 CSS px on 2x and 3x screens."""
    import struct

    from app.alerts.channels import LOGO_URL

    assert LOGO.is_file(), f"missing {LOGO}"
    assert LOGO_URL == "https://sliceapp.dev/logo.png"
    head = LOGO.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "logo.png is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    assert width == 192 and height >= 96
    assert LOGO.stat().st_size < 100 * 1024


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


def test_how_to_step_04_says_spend_is_an_estimate(how_to):
    """Phase 26: the spend tile is honest about where its number comes from."""
    assert "slice's estimate, worked out from token counts at list prices" in how_to
    assert "your Anthropic bill is the true figure" in how_to


def test_how_to_page_has_no_em_dash(how_to):
    assert "\u2014" not in how_to


def test_how_to_page_has_copy_buttons_and_one_script(how_to):
    assert how_to.count('class="copy"') >= 8
    assert how_to.count("<script>") == 1


def test_how_to_page_shows_the_current_cli_commands(how_to):
    """The install and login blocks match the CLI as it ships (0.2.1: hosted default)."""
    version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert "pipx install slice-gateway" in how_to
    assert "pip install slice-gateway" not in how_to
    assert "slice --version" in how_to
    assert f"slice-gateway {version}" in how_to
    assert "pip show" not in how_to
    # Login is the bare command; the hosted gateway is the default, not a flag.
    assert "slice login" in how_to
    assert "--base-url https://api.sliceapp.dev" not in how_to
    # Self-hosted users get the one line that points the CLI at their own box, in the
    # closing section rather than the login step.
    assert "slice login --base-url http://localhost:8080" in how_to
    assert "SLICE_BASE_URL" in how_to
    assert "Self-hosted?" not in how_to
    assert how_to.index("Running your own slice?") > how_to.index("Read the code, or run it on your own box.")


def test_site_header_links_to_how_to():
    assert 'href="https://sliceapp.dev/how-to"' in INDEX.read_text(encoding="utf-8")


def test_every_get_started_link_goes_to_the_install_section(how_to):
    """Get started means the how-to page; the dashboard is reached from that page's own
    Open the dashboard button at the bottom. Both pages are checked, and the page's own
    section nav still has its install anchor."""
    index = INDEX.read_text(encoding="utf-8")
    assert 'id="install"' in how_to
    links = re.findall(r'<a[^>]*href="([^"]*)"[^>]*>\s*(?:<svg[^>]*>.*?</svg>)?\s*Get started\s*</a>', index + how_to, re.S)
    assert len(links) == 3, links
    assert set(links) == {"https://sliceapp.dev/how-to"}
    assert 'href="https://sliceapp.dev/dashboard">' in how_to and "Open the dashboard" in how_to


def test_dashboard_links_point_at_the_apex_dashboard(how_to):
    """Phase 30: the dashboard lives at sliceapp.dev/dashboard. Every link that means the
    dashboard goes there; api.sliceapp.dev stays the gateway address in the tool blocks,
    never an href."""
    index = INDEX.read_text(encoding="utf-8")
    for page in (how_to, index):
        assert 'href="https://api.sliceapp.dev' not in page
    assert how_to.count('href="https://sliceapp.dev/dashboard"') >= 3
    assert 'href="https://sliceapp.dev/dashboard" target="_blank" rel="noopener">slice dashboard</a>' in index
    assert "Open the dashboard at <a href=\"https://sliceapp.dev/dashboard\"" in how_to
    assert "api.sliceapp.dev" in how_to  # still the gateway address for tools


def test_dashboard_header_links_to_how_to():
    app = DASHBOARD_APP.read_text(encoding="utf-8")
    assert 'href="https://sliceapp.dev/how-to"' in app
    assert 'target="_blank"' in app



def _section(how_to: str, section_id: str) -> str:
    start = how_to.index(f'<section class="block" id="{section_id}">')
    return how_to[start:how_to.index("</section>", start)]


def test_install_section_is_pipx_on_three_tabs(how_to):
    """Section 01 installs through pipx on each of the three OS tabs of the page's own tab
    component (the same three icons, in the same order); nothing on the page says pip
    install. Section 08 removes it with pipx uninstall on the same tabs."""
    install = _section(how_to, "install")
    tabs = re.search(r'<div class="tabs"[^>]*>(.*?)</div>', install)
    assert tabs and re.findall(r'data-os="(\w+)"', tabs.group(1)) == ["mac", "linux", "win"]
    assert re.findall(r'src="([^"]+)"', tabs.group(1)) == ["apple.png", "tux.png", "windows.svg"]
    pres = dict(re.findall(r'<pre data-os="(\w+)"[^>]*>(.*?)</pre>', install, re.S))
    assert set(pres) == {"mac", "linux", "win"}
    for os_name, body in pres.items():
        assert "pipx install slice-gateway" in body, os_name
        assert "slice --version" in body, os_name
        assert "pip install slice-gateway" not in body, os_name
    assert "brew install pipx" in pres["mac"] and "sudo apt install pipx" in pres["linux"]
    assert "py -m pip install --user pipx" in pres["win"] and "py -m pipx ensurepath" in pres["win"]
    # One fill box per tab, flipped by the same switch, and the expected version line.
    assert re.findall(r'<div class="fill" data-os="(\w+)"', install) == ["mac", "linux", "win"]
    assert "Expected output of the last line" in install
    assert "Use pipx." in install and "externally managed environment" in install

    uninstall = _section(how_to, "uninstall")
    assert uninstall.count("pipx uninstall slice-gateway") == 2
    assert "pip uninstall" not in uninstall
    assert 'data-os="mac linux"' in uninstall and 'data-os="win"' in uninstall
    # The three icons are still what the tabs use, on every tabbed block.
    for icon in ("apple.png", "tux.png", "windows.svg"):
        assert how_to.count(f'src="{icon}"') >= 4, icon



def test_no_link_points_at_the_html_address(how_to):
    """Phase 31: the how-to page is /how-to; every link on both pages uses the clean URL
    (with its #section where it has one), never how-to.html."""
    index = INDEX.read_text(encoding="utf-8")
    for page in (index, how_to):
        assert 'href="how-to.html' not in page and "how-to.html" not in re.findall(r'href="([^"]+)"', page).__str__()
        assert 'href="https://sliceapp.dev/how-to' in page
    assert 'href="https://sliceapp.dev/how-to"' in index and 'href="https://sliceapp.dev/how-to"' in how_to


def test_install_section_lists_the_versions_under_the_pipx_paragraph(how_to):
    install = how_to[how_to.index('id="install"'):how_to.index('id="login"')]
    pipx = install.index("Use pipx.")
    items = re.findall(r"<li><b>(\w+):</b>(.*?)</li>", install[pipx:], re.S)
    assert [name for name, _ in items][:3] == ["Mac", "Linux", "Windows"]
    text = " ".join(body for _, body in items[:3])
    for needle in ("macOS 13 Ventura", "Homebrew Python 3.11", "is 3.9", "Debian 12", "Ubuntu 23.04", "24.04 LTS",
                   "Fedora 38", "Arch since 2023", "Windows 10 and 11", "python.org", "never refuses pip"):
        assert needle in text, needle
    assert install.index("<ul>", pipx) < install.index('<div class="term">', pipx)
    assert "This applies to" not in install


def test_claude_code_step_starts_with_before_you_start(how_to):
    """Section 03's Claude Code part opens with the two prerequisites on the page's own tab
    component: the install line per OS plus the version check, the quickstart link, the
    console link, and then the three export lines as before."""
    tools = _section(how_to, "tools")
    start = tools.index("<h3>Claude Code</h3>")
    part = tools[start:tools.index("Set three variables", start)]
    assert "You need two things first: Claude Code on this machine, and an Anthropic API key." in part
    tabs = re.search(r'<div class="tabs"[^>]*>(.*?)</div>', part)
    assert tabs and re.findall(r'data-os="(\w+)"', tabs.group(1)) == ["mac", "linux", "win"]
    assert re.findall(r'src="([^"]+)"', tabs.group(1)) == ["apple.png", "tux.png", "windows.svg"]
    pres = re.findall(r'<pre data-os="([^"]+)"[^>]*>(.*?)</pre>', part, re.S)
    expected = {
        "mac": "curl -fsSL https://claude.ai/install.sh | bash",
        "linux": "curl -fsSL https://claude.ai/install.sh | bash",
        "win": "irm https://claude.ai/install.ps1 | iex",
    }
    for os_name, line in expected.items():
        body = next(body for names, body in pres if os_name in names.split())
        assert line in body and "claude --version" in body, os_name
    assert 'href="https://code.claude.com/docs/en/quickstart" target="_blank" rel="noopener"' in part
    assert 'href="https://console.anthropic.com" target="_blank" rel="noopener"' in part
    assert "Needs macOS 13, Windows 10 (1809) or Ubuntu 20.04 and newer." in part
    assert "you need the console key" in part
    # The three export lines still follow, after this part.
    after = tools[tools.index("Set three variables", start):]
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        assert f"export</span> {name}=" in after, name


def test_tools_step_says_what_slice_sees_and_keeps(how_to):
    tools = _section(how_to, "tools")
    assert "<p><b>What slice sees, and what it keeps.</b></p>" in tools
    for line in (
        "Your files never go to slice.",
        "What it keeps: which model, how many tokens, what it cost, and when.",
        "What it does not keep: your prompt, the answer, your code, or your Anthropic key.",
        "Why you can trust that: the request table has those columns and nothing else,",
    ):
        assert line in tools, line
    assert '<a href="https://github.com/jjk30/slice" target="_blank" rel="noopener">open on GitHub</a>' in tools
    assert "slice reads each request only to route it" not in how_to
    # It sits before the Claude Code part, in the section's intro.
    assert tools.index("What slice sees") < tools.index("<h3>Claude Code</h3>")
