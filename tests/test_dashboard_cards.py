"""The sign-in card's "New here" line, the sign-out card, and the first-request card.

Same two layers as test_dashboard_tile: source checks that always run, and real renders
of the components through Vite and Vue's server renderer (the toolchain that builds the
dashboard), which skip when node or the dashboard's node_modules are missing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
COMPONENTS = DASHBOARD / "src" / "components"
APP = DASHBOARD / "src" / "App.vue"
AUTH = DASHBOARD / "src" / "auth.js"

HOW_TO_INSTALL = "https://sliceapp.dev/how-to.html#install"

# Runs from the dashboard directory. Each case is {component, props}; the rendered HTML
# leaves as a JSON list in the same order. LoginScreen reads window.location at render
# time, so a bare window is provided for the server renderer.
RENDER = """
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
globalThis.window ??= { location: { search: '', href: '' } }
const cases = JSON.parse(process.env.CARD_CASES)
const server = await createServer({
  configFile: 'vite.config.js', appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, watch: null },
})
try {
  const out = []
  for (const { component, props } of cases) {
    const mod = await server.ssrLoadModule('/src/components/' + component)
    out.push(await renderToString(createSSRApp(mod.default, props)))
  }
  process.stdout.write(JSON.stringify(out))
} finally {
  await server.close()
}
"""


def _render(cases: list[dict]) -> list[str]:
    node = shutil.which("node")
    if node is None or not (DASHBOARD / "node_modules" / "vite").is_dir():
        pytest.skip("node and dashboard/node_modules are needed to render the cards")
    result = subprocess.run(
        [node, "--input-type=module", "-e", RENDER],
        cwd=DASHBOARD, capture_output=True, text=True, timeout=120,
        env={"PATH": str(Path(node).parent), "CARD_CASES": json.dumps(cases), "HOME": str(Path.home())},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- source checks ------------------------------------------------------------------


def test_login_screen_source_carries_the_new_here_line():
    source = (COMPONENTS / "LoginScreen.vue").read_text(encoding="utf-8")
    assert "New here? Install the CLI first, about three minutes." in source
    assert f'href="{HOW_TO_INSTALL}"' in source
    # Under the GitHub button and above the terminal note.
    assert source.index("Sign in with GitHub") < source.index("New here?") < source.index("The terminal uses")


def test_signing_out_source_matches_the_login_card():
    source = (COMPONENTS / "SigningOut.vue").read_text(encoding="utf-8")
    assert 'class="login"' in source and 'class="login-card card"' in source
    assert 'src="/favicon.png"' in source and 'width="28" height="28"' in source
    assert "prefers-reduced-motion" in source
    assert "failed:" in source


def test_app_drives_the_sign_out_card_from_the_real_logout_call():
    app = APP.read_text(encoding="utf-8")
    auth = AUTH.read_text(encoding="utf-8")
    assert "return reached" in auth
    assert "const signingOut = ref(false)" in app
    assert "if (signingOut.value) return 'signing-out'" in app
    assert "<SigningOut v-else-if=\"view === 'signing-out'\" :failed=\"logoutFailed\" />" in app
    body = app[app.index("async function onLogout"):app.index("const RECENT_LIMIT")]
    # The card goes up before the stream stops, and is released only after logout() resolves.
    assert body.index("signingOut.value = true") < body.index("stopLive()") < body.index("await logout()")
    assert body.index("await logout()") < body.index("signingOut.value = false")
    assert "SIGNOUT_MIN_MS = 4000" in app and "SIGNOUT_FAILED_MS = 1500" in app


def test_app_shows_the_first_request_card_only_for_a_loaded_zero():
    app = APP.read_text(encoding="utf-8")
    assert "if (!summary.value || failed.value) return null" in app
    assert "const firstRequest = computed(() => requestCount.value === 0)" in app
    assert '<FirstRequestCard class="span-4" :requests="requestCount" />' in app
    assert '<div class="kpis" :class="{ dimmed: firstRequest }">' in app
    assert '<ModelsChart v-if="!firstRequest"' in app
    assert '<RecentCalls v-if="!firstRequest"' in app
    assert app.index("<FirstRequestCard") < app.index('<div class="kpis"')


def test_dimmed_kpi_row_is_forty_five_percent():
    css = (DASHBOARD / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".kpis.dimmed {\n  opacity: .45;\n}" in css


# --- real renders --------------------------------------------------------------------


def test_cards_render():
    signing_out, signing_out_failed, login, first_zero, first_three = _render([
        {"component": "SigningOut.vue", "props": {}},
        {"component": "SigningOut.vue", "props": {"failed": True}},
        {"component": "LoginScreen.vue", "props": {}},
        {"component": "FirstRequestCard.vue", "props": {"requests": 0}},
        {"component": "FirstRequestCard.vue", "props": {"requests": 3}},
    ])

    # SigningOut: heading, the saved-data sentence, a turning spinner.
    assert "Signing you out" in signing_out
    assert "Your spend, keys, and findings stay saved. You&#39;ll see them again next time you sign in." in signing_out
    assert 'class="login-card card"' in signing_out and 'src="/favicon.png"' in signing_out
    assert re.search(r'<span class="spinner"[^>]*>', signing_out) and "still" not in signing_out

    # SigningOut with failed=true: the device-only sentence, and the spinner stopped.
    assert "Signing you out" in signing_out_failed
    assert "Couldn&#39;t reach the server, so you&#39;re signed out on this device only." in signing_out_failed
    assert "stay saved" not in signing_out_failed
    # The renderer may order the static and dynamic classes either way.
    assert re.search(r'<span class="(spinner still|still spinner)"[^>]*>', signing_out_failed)

    # LoginScreen: the New here line with its link, still under the button.
    assert "New here? Install the CLI first, about three minutes." in login
    # Scoped styles stamp a data-v attribute on every element, so match around it.
    assert re.search(rf'<a href="{re.escape(HOW_TO_INSTALL)}"[^>]*>Set up slice</a>', login)
    assert login.index("Sign in with GitHub") < login.index("New here?") < login.index("The terminal uses")

    # First-request card: rendered at zero with its lines and link, nothing at three.
    assert "Send your first request through slice" in first_zero
    assert "Nothing has come through yet." in first_zero
    assert "export ANTHROPIC_BASE_URL=https://api.sliceapp.dev" in first_zero
    assert re.search(r'<span class="ph"[^>]*>\(your own Anthropic key\)</span>', first_zero)
    assert re.search(r'<span class="ph"[^>]*>\(your slice key\)</span>', first_zero)
    assert f'href="{HOW_TO_INSTALL}"' in first_zero and "Full setup, step by step" in first_zero
    assert "slice use claude-code" in first_zero
    assert ">Copy</button>" in first_zero
    assert "Send your first request" not in first_three
    assert first_three.strip() == "<!---->"
