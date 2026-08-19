# slice MCP server (phase 14)

A standalone [MCP](https://modelcontextprotocol.io) stdio server that exposes a running
slice gateway's data as tools inside Claude Code. It is a **thin adapter**: it only makes
short async HTTP calls to slice's existing gateway API and shapes the JSON into compact,
human-readable text. It contains no gateway logic, no database access, and no business
rules — every number comes from an endpoint the gateway already serves.

## Run

```bash
pip install -r mcp_server/requirements.txt
SLICE_BASE_URL=http://localhost:8080 SLICE_API_KEY=slk_... python -m mcp_server
```

- `SLICE_BASE_URL` — the gateway URL (default `http://localhost:8080`).
- `SLICE_API_KEY` — your slice key, sent as `Authorization: Bearer <key>` (the same header
  the gateway's phase-12 auth reads). **Optional**: unset works against a gateway running
  in local/unlocked mode. If a call comes back `401`, the tool tells you to set the key.

## Register in Claude Code

```bash
claude mcp add slice -- python -m mcp_server
```

(set `SLICE_BASE_URL` / `SLICE_API_KEY` in the environment Claude Code launches it with.)

## Tools

Reads (free, no confirmation):

| tool | gateway endpoint | shows |
| --- | --- | --- |
| `get_spend` | `GET /dashboard/teams` | current-month spend vs budget, warn ratio, cap-hit |
| `list_rules` | `GET /admin/rules` | the account's switch rules |
| `get_recent_requests` | `GET /dashboard/recent?limit=N` | last N requests (N≤50) |
| `get_eval_summary` | `GET /admin/eval/summary` | RAGAS pass rate |

Writes (two-call confirm handshake — call once to preview, again with `confirm=true` to apply):

| tool | gateway endpoint |
| --- | --- |
| `add_rule` | `POST /admin/rules` |
| `delete_rule` | `DELETE /admin/rules/{id}` |

Every read returns clean text and fails gracefully: a gateway that is down yields
`slice gateway not running at <url>`, never a stack trace.

> **Note:** `/dashboard/recent` does not expose `latency_ms` (the column exists in the
> `requests` table but the route omits it). `get_recent_requests` reports the fields the
> endpoint does return; no gateway route was changed to add latency.

## Tests

```bash
python -m pytest tests/test_mcp_server.py
```

The gateway is always faked with `respx` — no live network.
