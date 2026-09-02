"""Guard: the production dashboard bundle must be same-origin (no baked-in gateway host).

The gateway serves dashboard/dist same-origin (app.main.DASHBOARD_DIST), so every URL the
dashboard builds — the GitHub login redirect, logout, the slice-key card, the SSE stream,
every fetch — must be relative. apiBase() returns "" when VITE_API_BASE_URL is unset/empty,
which is the only correct value for a production build; a stray VITE_API_BASE_URL (e.g. a
localhost value left in .env, which Vite loads in every mode) would bake an absolute host
into the shipped assets and break production. This test fails if that regresses.

It scans the committed/built dist/assets/*.js — the exact files that ship. If the bundle
has not been built (no dist), the test skips rather than failing, so a source-only checkout
still passes; run `cd dashboard && npm run build` to produce it.
"""

from pathlib import Path

import pytest

from app.main import DASHBOARD_DIST

# The forbidden host is the local-dev gateway origin; if it appears in a built asset the
# dashboard would point production browsers at localhost instead of the serving origin.
FORBIDDEN = "localhost:8080"

ASSETS_DIR = DASHBOARD_DIST / "assets"


def _built_js():
    if not ASSETS_DIR.is_dir():
        return None
    return sorted(ASSETS_DIR.glob("*.js"))


def test_production_bundle_has_no_gateway_host():
    js_files = _built_js()
    if not js_files:
        pytest.skip(
            f"no built dashboard assets at {ASSETS_DIR}; run `cd dashboard && npm run build`"
        )
    offenders = []
    for path in js_files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        count = text.count(FORBIDDEN)
        if count:
            offenders.append(f"{path.name}: {count}")
    assert not offenders, (
        f"production dashboard bundle must be same-origin, but {FORBIDDEN!r} is baked into: "
        + ", ".join(offenders)
        + ". Ensure VITE_API_BASE_URL is empty for the production build (dashboard/.env.production)."
    )
