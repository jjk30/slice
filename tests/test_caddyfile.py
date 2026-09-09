"""The EC2 Caddyfile (infra/ec2/files/Caddyfile), phase 30: the dashboard on the apex.

The file is static text (the hostnames come from the environment), so these are text
checks on the two site blocks: the apex proxies the app paths to the gateway ahead of
the static site, streams the live events path unbuffered, and the api host sends a
browser that lands on the root or an app path to the apex dashboard for good while
still hiding /metrics.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = ROOT / "infra" / "ec2" / "files" / "Caddyfile"

# Every API prefix the dashboard calls same-origin (dashboard/src), plus the two pages
# and the bundle. If a new prefix is added to the app, it must be added here and in
# the Caddyfile or the apex would serve a 404 page for it.
APP_PATHS = ["/dashboard", "/settings", "/dashboard/*", "/assets/*", "/auth/*", "/scanner/*", "/admin/*", "/account/*"]


def _block(text: str, name: str) -> str:
    start = text.index(name + " {")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_caddyfile_has_no_em_dash():
    assert chr(0x2014) not in CADDYFILE.read_text(encoding="utf-8")


def test_apex_proxies_the_app_paths_before_the_static_site():
    apex = _block(CADDYFILE.read_text(encoding="utf-8"), "sliceapp.dev")
    matcher = re.search(r"@app path (.+)", apex)
    assert matcher, "no @app matcher in the apex block"
    assert matcher.group(1).split() == APP_PATHS
    assert "reverse_proxy @app gateway:8080" in apex
    # The live stream is proxied on its own, unbuffered, and matched before the app paths.
    assert "@events path /dashboard/events" in apex
    events = apex[apex.index("reverse_proxy @events gateway:8080"):]
    assert "flush_interval -1" in events[: events.index("}") + 1]
    assert apex.index("reverse_proxy @events") < apex.index("reverse_proxy @app") < apex.index("file_server")
    # The static site is still the fallback, from the mounted folder.
    assert "root * /srv/slice-site" in apex and "file_server" in apex


def test_apex_serves_clean_urls_for_the_static_pages():
    """Phase 31: /how-to serves how-to.html through try_files, placed right before
    file_server, and the old .html address is a permanent redirect placed before the
    app path matcher."""
    apex = _block(CADDYFILE.read_text(encoding="utf-8"), "sliceapp.dev")
    assert "try_files {path} {path}.html" in apex
    assert "redir /how-to.html /how-to permanent" in apex
    assert apex.index("redir /how-to.html") < apex.index("@app path")
    directive = "\n    try_files {path} {path}.html\n"
    assert apex.index("reverse_proxy @app") < apex.index(directive) < apex.index("\n    file_server\n")


def test_api_host_sends_the_pages_to_the_apex_and_keeps_metrics_hidden():
    api = _block(CADDYFILE.read_text(encoding="utf-8"), "{$API_DOMAIN}")
    assert "@app path / /dashboard /settings" in api
    assert "redir @app https://sliceapp.dev/dashboard permanent" in api
    assert "@metrics path /metrics /metrics/*" in api and "respond @metrics 404" in api
    # The API itself still proxies: the redirect is only the three exact paths.
    assert "reverse_proxy gateway:8080" in api
    assert api.index("respond @metrics 404") < api.index("redir @app") < api.index("reverse_proxy gateway:8080")


def test_www_still_redirects_to_the_apex():
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "www.sliceapp.dev {\n    redir https://sliceapp.dev{uri} permanent\n}" in text
