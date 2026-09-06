"""The guardrail tile's phase-28 split line: "{email} email, {gateway} gateway".

Two layers. A source check that always runs (the template carries the line and its
"none yet" wording), and a real render: the component is compiled and rendered with
Vue's server renderer through Vite, the same toolchain that builds the dashboard, so
the test sees the HTML a browser would. The render skips when node or the dashboard's
node_modules are missing, like the kubectl lint in test_k8s_manifests, so a Python-only
checkout still passes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
TILE = DASHBOARD / "src" / "components" / "GuardrailsTile.vue"

# Runs from the dashboard directory (bare imports resolve against its node_modules);
# the props for each case arrive as JSON in TILE_CASES, the rendered HTML leaves as JSON.
RENDER = """
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
const cases = JSON.parse(process.env.TILE_CASES)
const server = await createServer({
  configFile: 'vite.config.js', appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, watch: null },
})
try {
  const mod = await server.ssrLoadModule('/src/components/GuardrailsTile.vue')
  const out = []
  for (const props of cases) out.push(await renderToString(createSSRApp(mod.default, props)))
  process.stdout.write(JSON.stringify(out))
} finally {
  await server.close()
}
"""


def _guardrails(blocked: int, email: int, gateway: int) -> dict:
    return {
        "guardrails": {
            "total": blocked, "blocked": blocked, "errors": 0, "blocked_by_rail": [],
            "blocked_by_source": {"email": email, "gateway": gateway},
        }
    }


def _render(cases: list[dict]) -> list[str]:
    node = shutil.which("node")
    if node is None or not (DASHBOARD / "node_modules" / "vite").is_dir():
        pytest.skip("node and dashboard/node_modules are needed to render the tile")
    result = subprocess.run(
        [node, "--input-type=module", "-e", RENDER],
        cwd=DASHBOARD, capture_output=True, text=True, timeout=120,
        env={"PATH": str(Path(node).parent), "TILE_CASES": json.dumps(cases), "HOME": str(Path.home())},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tile_source_carries_the_split_line():
    source = TILE.read_text(encoding="utf-8")
    assert "blocked_by_source" in source
    assert "none yet" in source
    assert 'class="kpi-sub by-source"' in source


def test_tile_renders_the_split_line():
    with_blocks, none_yet, loading = _render([_guardrails(3, 2, 1), _guardrails(0, 0, 0), {"guardrails": None}])
    # The total as before, then the split on its own small line under it.
    assert '<p class="tone-cherry kpi-value">3</p>' in with_blocks
    assert with_blocks.index("kpi-value") < with_blocks.index("by-source")
    assert '<p class="kpi-sub by-source">2 email, 1 gateway</p>' in with_blocks
    # Both zero: "none yet", in ink (no cherry).
    assert '<p class="kpi-value">0</p>' in none_yet
    assert '<p class="kpi-sub by-source">none yet</p>' in none_yet
    # Still loading: no split line at all.
    assert "Loading" in loading and "by-source" not in loading
