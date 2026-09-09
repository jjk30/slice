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
    assert chr(0x2014) not in how_to


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
    assert 'href="https://sliceapp.dev/dashboard"' in how_to and "Open the dashboard" in how_to


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


# The screenshots on the page, in page order: one in step 01, then the eight of step 02.
STEP_02_SCREENSHOTS = [
    "02-login-terminal.png",
    "02-github-device.png",
    "02-github-code.png",
    "02-github-authorize.png",
    "02-github-done.png",
    "02-logged-in.png",
    "02-dashboard-signin.png",
    "02-dashboard.png",
]
# Step 03: the three export lines, the curl run twice, and the Recent calls panel.
STEP_03_SCREENSHOTS = ["03-slice-use.png", "03-curl-twice.png", "03-recent-calls.png"]
# Step 07: the alert email in Gmail, then the three kinds of reply.
STEP_07_SCREENSHOTS = ["07-alert-email.png", "07-reply-own-data.png", "07-reply-general.png", "07-reply-blocked.png"]
SCREENSHOTS = ["01-install.png", *STEP_02_SCREENSHOTS, *STEP_03_SCREENSHOTS, *STEP_07_SCREENSHOTS]

STEP_02_LABELS = [
    "Terminal after slice login",
    "GitHub: Device Activation, click Continue",
    "GitHub: enter the code",
    "GitHub: Authorize slice",
    "GitHub: done",
    "Terminal: logged in, with your dashboard link",
    "Dashboard: click Log in",
    "Dashboard: signed in",
]

def _img_tags(how_to: str) -> dict[str, str]:
    imgs = {}
    for tag in re.findall(r"<img [^>]*>", how_to):
        src = re.search(r'src="img/([^"]+)"', tag)
        if src:
            imgs[src.group(1)] = tag
    return imgs


def test_how_to_page_shows_the_screenshots(how_to):
    """Every <img> under website/img names a file that exists, has a non-empty alt, and
    declares its width and height so the page does not jump while the pictures load.
    The one Mac caption sits under the first picture of step 01."""
    imgs = _img_tags(how_to)
    assert set(imgs) == set(SCREENSHOTS), sorted(imgs)
    for name in SCREENSHOTS:
        tag = imgs[name]
        assert (WEBSITE / "img" / name).is_file(), name
        alt = re.search(r'alt="([^"]*)"', tag)
        assert alt and alt.group(1).strip(), f"{name}: empty alt"
        for attr in ("width", "height"):
            value = re.search(rf'\b{attr}="(\d+)"', tag)
            assert value and int(value.group(1)) > 0, f"{name}: missing {attr}"
        assert 'loading="lazy"' in tag, name
    positions = [how_to.index(f'src="img/{n}"') for n in SCREENSHOTS]
    assert positions == sorted(positions)
    # One caption per step: the Mac line under the first terminal picture of steps 01 and
    # 03, the GitHub line under the first GitHub page picture of step 02.
    caption = "Taken on my Mac. Your username and a few details will look different; the commands are the same."
    assert how_to.count(caption) == 2
    assert positions[0] < how_to.index(caption) < positions[1]
    first_03, second_03 = (how_to.index(f'src="img/{n}"') for n in STEP_03_SCREENSHOTS[:2])
    assert first_03 < how_to.rindex(caption) < second_03
    github = "The GitHub pages look the same on every system."
    assert how_to.count(github) == 1
    assert how_to.index('src="img/02-github-device.png"') < how_to.index(github) < how_to.index('src="img/02-github-code.png"')
    gmail = "Taken from my Gmail. Your findings and numbers will differ."
    assert how_to.count(gmail) == 1
    first_07, second_07 = (how_to.index(f'src="img/{n}"') for n in STEP_07_SCREENSHOTS[:2])
    assert first_07 < how_to.index(gmail) < second_07
    assert how_to.count("<figcaption>") == 4
    assert "Screenshots from a Mac" not in how_to
    tools = _section(how_to, "tools")
    assert re.findall(r'src="img/([^"]+)"', tools) == STEP_03_SCREENSHOTS
    for name in STEP_03_SCREENSHOTS:
        at = tools.index(f'src="img/{name}"')
        # Outside every OS tab pane: the last <pre> opened before the picture is closed.
        assert tools.rfind("<pre", 0, at) < tools.rfind("</pre>", 0, at), name
        assert tools[tools.rfind("<figure", 0, at):at].startswith('<figure class="shot">'), name
    assert "shot-row" not in how_to and "shot-gallery" not in how_to


def test_login_step_has_the_nine_pictures_in_order_with_labels(how_to):
    """Step 02 for CLI 0.2.2: the eight pictures of the login walk (the ninth picture
    on the page counting the install one) come in the order of the steps the user takes,
    each with its label above it, and the username note sits after the terminal ones."""
    login = _section(how_to, "login")
    names = re.findall(r'src="img/([^"]+)"', login)
    assert names == STEP_02_SCREENSHOTS
    assert re.findall(r'<div class="lab">([^<]+)</div>', login) == STEP_02_LABELS
    for name in STEP_02_SCREENSHOTS:
        assert (WEBSITE / "img" / name).is_file(), name
    note = login.index("You will see your own GitHub username here.")
    assert login.index('src="img/02-logged-in.png"') < note < login.index('src="img/02-dashboard-signin.png"')


def test_login_block_has_the_three_os_tabs_and_ends_with_the_dashboard_line(how_to):
    """The slice login block uses the page's own tab component with the same three icons,
    and every tab's mock output ends with the dashboard line the 0.2.2 CLI prints."""
    login = _section(how_to, "login")
    block = login[login.index('<div class="term">'):login.index("</div>", login.index("</pre>\n    </div>")) + 6]
    tabs = re.search(r'<div class="tabs"[^>]*>(.*?)</div>', block)
    assert tabs and re.findall(r'data-os="(\w+)"', tabs.group(1)) == ["mac", "linux", "win"]
    assert re.findall(r'src="([^"]+)"', tabs.group(1)) == ["apple.png", "tux.png", "windows.svg"]
    pres = re.findall(r'<pre data-os="([^"]+)"[^>]*>(.*?)</pre>', block, re.S)
    covered = {os_name for names, _ in pres for os_name in names.split()}
    assert covered == {"mac", "linux", "win"}
    for names, body in pres:
        text = re.sub(r"<[^>]+>", "", body)
        # The command alone: the copy button copies only what the reader types.
        assert text.strip() in ("$ slice login", "&gt; slice login"), names
    assert 'src="img/' not in block  # pictures sit outside the tab panes
    # The output sits once under the tabs (it is the same on every OS), in the light box,
    # with the username and the code as placeholders and the dashboard line last.
    see = _see_blocks(login)[0]
    assert see[0] is None
    assert "WXYZ-1234" in see[1] and "jjk30" not in see[1]
    assert see[1].rstrip().endswith("Logged in as your username\n\nYour dashboard: https://sliceapp.dev/dashboard")
    assert login.index("$</span> slice login") < login.index("What you should see") < login.index('src="img/02-login-terminal.png"')


def test_login_step_says_the_dashboard_is_a_separate_door(how_to):
    login = _section(how_to, "login")
    assert "<h3>Now open your dashboard</h3>" in login
    intro = login[:login.index('<div class="term">')]
    assert "opens the link in your browser" in intro and "Your dashboard" not in intro
    door = login[login.index("<h3>Now open your dashboard</h3>"):login.index("<h3>Your slice key</h3>")]
    assert "two separate doors" in door and "one click, no forms" in door
    assert 'href="https://sliceapp.dev/dashboard" target="_blank" rel="noopener">sliceapp.dev/dashboard</a>' in door
    # Now open your dashboard comes after the terminal pictures and before Your slice key.
    assert login.index('src="img/02-logged-in.png"') < login.index("<h3>Now open your dashboard</h3>") < login.index("<h3>Your slice key</h3>")


# --- Command blocks hold commands; the output sits in a "What you should see" box ------

# A line of output the CLI prints, never something the reader should type or copy.
OUTPUT_STARTS = ("Logged in as", "Waiting for", "Your dashboard", "To finish", "and enter this code")


def _command_blocks(how_to: str) -> list[tuple[str, str]]:
    """(data-os or "", text) of every <pre> inside a dark .term block, tags stripped."""
    blocks = []
    for term in re.findall(r'<div class="term">(.*?)\n    </div>', how_to, re.S):
        for attrs, body in re.findall(r"<pre([^>]*)>(.*?)</pre>", term, re.S):
            os_names = re.search(r'data-os="([^"]+)"', attrs)
            blocks.append((os_names.group(1) if os_names else "", re.sub(r"<[^>]+>", "", body)))
    return blocks


def _see_blocks(html: str) -> list[tuple[str | None, str]]:
    """(data-os or None, output text) of every "What you should see" box, in page order."""
    found = []
    pattern = r'<div class="term see"([^>]*)>\s*<pre>(.*?)</pre>'
    for attrs, body in re.findall(pattern, html, re.S):
        os_names = re.search(r'data-os="([^"]+)"', attrs)
        found.append((os_names.group(1) if os_names else None, body))
    return found


def test_no_command_block_carries_output_lines(how_to):
    blocks = _command_blocks(how_to)
    assert len(blocks) >= 12
    for os_names, text in blocks:
        for line in text.splitlines():
            assert not line.strip().startswith(OUTPUT_STARTS), (os_names, line)
    assert "jjk30" not in "".join(text for _, text in blocks)


def test_what_you_should_see_boxes_sit_under_the_split_blocks(how_to):
    """Two blocks were split: slice login (one box, the output is the same everywhere) and
    slice init (one box per OS pane, the config path differs). Each box is a dark terminal
    block with no bar at all (no dots, no tabs, no copy button), under one small heading in
    the page font: one above the login box, one above the init pair. Every box uses
    placeholders for the username, the code, and the account number."""
    login = _section(how_to, "login")
    boxes = _see_blocks(login)
    assert [os_names for os_names, _ in boxes] == [None, "mac linux", "win"]
    assert _see_blocks(how_to) == boxes
    for _, text in boxes:
        assert "jjk30" not in text and "your username" in text
    assert "/Users/you/.slice/config.json" in boxes[1][1] and "C:\\Users\\you\\.slice\\config.json" in boxes[2][1]
    init = login[login.index("$</span> slice init"):]
    assert init.index("</div>") < init.index("What you should see")
    # One heading per split block, directly above its box (or the pair), none inside a box.
    heading = '<h4 class="see-head">What you should see</h4>'
    assert how_to.count(heading) == 2
    assert how_to.count(">What you should see<") == 2  # the two headings, no label anywhere else
    for opener in ('<div class="term see">', '<div class="term see" data-os="mac linux">'):
        before = how_to[:how_to.index(opener)].rstrip()
        assert before.endswith(heading), opener
    assert ".block h4.see-head{" in how_to
    assert '<div class="term see" data-os="win" hidden>' in login
    assert "account 1)" in boxes[1][1] and "account 1)" in boxes[2][1]
    assert "account 14" not in how_to
    boxes_html = re.findall(r'<div class="term see"[^>]*>.*?</pre>\s*</div>', how_to, re.S)
    assert len(boxes_html) == 3
    for box in boxes_html:
        assert 'class="bar"' not in box and "What you should see" not in box
        assert 'class="copy"' not in box and 'class="tabs"' not in box
        assert '<i class="r"></i>' not in box
    assert ".fill.see" not in how_to
    assert ".term.see[data-os]" in how_to  # the OS switch flips the per-OS boxes too


def test_alert_step_shows_gmail_pictures_instead_of_the_sample_block(how_to):
    """Step 07: the mocked-up email block is gone; four Gmail pictures stand in its place,
    each after the sentence it illustrates, with a label above and outside any term block."""
    alerts = _section(how_to, "alerts")
    assert "my-app-uploads" not in how_to
    assert '<div class="term">' not in alerts and 'class="copy"' not in alerts
    assert re.findall(r'src="img/([^"]+)"', alerts) == STEP_07_SCREENSHOTS
    assert re.findall(r'<div class="lab">([^<]+)</div>', alerts) == [
        "Gmail: the alert email",
        "Gmail: a question about your own account",
        "Gmail: a general AWS question",
        "Gmail: blocked by NeMo Guardrails",
    ]
    here = alerts.index("Here is one:</p>")
    footer = alerts.index("Every email ends with the same line")
    assert here < alerts.index('src="img/07-alert-email.png"') < footer
    reply = alerts.index("<h3>Reply with a question</h3>")
    off_topic = alerts.index("Anything off topic gets one line back")
    assert reply < alerts.index('src="img/07-reply-own-data.png"') < alerts.index('src="img/07-reply-general.png"') < off_topic
    assert off_topic < alerts.index('src="img/07-reply-blocked.png"')
    # Every sentence of the step is still there.
    for line in (
        "slice scans a connected AWS account once a day.",
        "Each finding is three short lines: what it is, why it matters, and the first thing to do.",
        "Every email ends with the same line:",
        "Some findings are on purpose, like a bucket that serves a public website.",
        "Reply to the email with a question about your own account.",
        "Anything off topic gets one line back: <b>Sorry, I can't help with that here.</b>",
    ):
        assert line in alerts, line

