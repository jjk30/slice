"""The Account budget panel's judge note under the used figure.

The Redis gate counter also carries the routing judge's own calls, which never become
a request row, so "budget used" can sit a little above the recorded spend. The panel
says so in one muted line, only when the meter is built on the counter
(``budget_source`` "redis") and the two figures differ. Rendered for real with Vue's
server renderer through Vite, like test_dashboard_tile; skips when node or the
dashboard's node_modules are missing so a Python-only checkout still passes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
PANEL = DASHBOARD / "src" / "components" / "TeamBudgets.vue"

# As the server renderer emits it: the apostrophe comes out as an entity.
NOTE = "Budget also counts the routing judge&#39;s own calls, so it can sit a little above real spend."

RENDER = """
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
const cases = JSON.parse(process.env.PANEL_CASES)
const server = await createServer({
  configFile: 'vite.config.js', appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, watch: null },
})
try {
  const mod = await server.ssrLoadModule('/src/components/TeamBudgets.vue')
  const { mergeTeams } = await server.ssrLoadModule('/src/merge.js')
  const out = []
  // A case may be {props} or {merge: [previous props, update props]}: the update is
  // merged over the previous payload the way App.vue does on a live refresh.
  for (const c of cases) {
    const props = c.merge ? { ...c.merge[0], data: mergeTeams(c.merge[0].data, c.merge[1].data) } : c
    out.push(await renderToString(createSSRApp(mod.default, props)))
  }
  process.stdout.write(JSON.stringify(out))
} finally {
  await server.close()
}
"""


def _teams(source: str, used: float, spend: float) -> dict:
    gate = used if source == "redis" else None
    return {
        "data": {
            "month": "2026-09",
            "budget_usd": 10.0,
            "budget_default": True,
            "default_budget_usd": 10.0,
            "warn_ratio": 0.8,
            "budget": {
                "account": "acme",
                "requests": 3,
                "spend_usd": spend,
                "unpriced_requests": 0,
                "budget_usd": 10.0,
                "gate_spend_usd": gate,
                "budget_used_usd": used,
                "budget_source": source,
                "remaining_usd": 10.0 - used,
            },
            "token_blend": {"input": 3, "output": 1},
            "token_estimates": [],
            "teams": [],
            "unattributed": {"requests": 0, "spend_usd": 0.0},
        }
    }


def _render(cases: list[dict]) -> list[str]:
    node = shutil.which("node")
    if node is None or not (DASHBOARD / "node_modules" / "vite").is_dir():
        pytest.skip("node and dashboard/node_modules are needed to render the panel")
    result = subprocess.run(
        [node, "--input-type=module", "-e", RENDER],
        cwd=DASHBOARD, capture_output=True, text=True, timeout=120,
        env={"PATH": str(Path(node).parent), "PANEL_CASES": json.dumps(cases), "HOME": str(Path.home())},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_panel_shows_the_judge_note_only_for_a_redis_gap():
    gap, equal, postgres = _render([
        _teams("redis", 0.0009, 0.0007),
        _teams("redis", 0.0007, 0.0007),
        _teams("postgres", 0.0007, 0.0007),
    ])
    # Redis counter above recorded spend: the note, muted, right under the used figure.
    assert NOTE in gap
    assert 'class="mono small muted judge-note"' in gap
    assert gap.index("used of") < gap.index("judge-note") < gap.index("progressbar")
    # Redis counter equal to recorded spend: nothing.
    assert "judge-note" not in equal and NOTE not in equal
    # Redis down, meter on recorded spend: nothing (the existing source note stays).
    assert "judge-note" not in postgres and NOTE not in postgres
    assert "gate counter unavailable" in postgres


def test_panel_keeps_the_note_when_a_live_update_leaves_fields_out():
    """A refetch after a live event goes through mergeTeams (App.vue): a payload that
    leaves out budget_source and spend_usd keeps the previous values, so the note stays.
    The same partial payload applied raw would drop it, which is the failure the merge
    guards against. A field the update carries still wins."""
    full = _teams("redis", 0.0009, 0.0007)
    partial = _teams("redis", 0.0011, 0.0007)
    del partial["data"]["budget"]["budget_source"]
    del partial["data"]["budget"]["spend_usd"]
    changed = _teams("redis", 0.0011, 0.0011)
    merged, raw, equalised = _render([
        {"merge": [full, partial]},
        partial,
        {"merge": [full, changed]},
    ])
    assert NOTE in merged and "$0.0011" in merged
    assert NOTE not in raw
    # The update carried spend_usd equal to the counter: the note goes, as it should.
    assert NOTE not in equalised

