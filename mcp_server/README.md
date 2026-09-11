# slice MCP server (phase 14)

A standalone [MCP](https://modelcontextprotocol.io) stdio server that exposes a running
slice gateway's data as tools inside Claude Code. It is a **thin adapter**: it only makes
short async HTTP calls to slice's existing gateway API and shapes the JSON into compact,
human-readable text. It contains no gateway logic, no database access, and no business
rules: every number comes from an endpoint the gateway already serves.

## Run

```bash
pip install -r mcp_server/requirements.txt
SLICE_BASE_URL=http://localhost:8080 SLICE_API_KEY=slk_... python -m mcp_server
```

- `SLICE_BASE_URL`: the gateway URL (default `http://localhost:8080`).
- `SLICE_API_KEY`: your slice key, sent as `Authorization: Bearer <key>` (the same header
  the gateway's phase-12 auth reads). **Optional**: unset works against a gateway running
  in local/unlocked mode. If a call comes back `401`, the tool tells you to set the key.

## Register in Claude Code

```bash
claude mcp add slice -- python -m mcp_server
```

(set `SLICE_BASE_URL` / `SLICE_API_KEY` in the environment Claude Code launches it with.)

## Tools

Reads (free, no approval needed):

| tool | gateway endpoint | shows |
| --- | --- | --- |
| `get_spend` | `GET /dashboard/teams` | current-month spend vs budget, warn ratio, cap-hit |
| `list_rules` | `GET /admin/rules` | the account's switch rules |
| `get_recent_requests` | `GET /dashboard/recent?limit=N` | last N requests (N<=50) |
| `get_eval_summary` | `GET /admin/eval/summary` | RAGAS pass rate |
| `get_action_status` | `GET /actions/{id}` | where a proposed write stands: pending, approved (with the rule id it applied), rejected, expired |

Writes (phase 32: proposed by the agent, approved by a person):

| tool | gateway endpoint |
| --- | --- |
| `add_rule` | `POST /actions/propose` with `{"kind": "add_rule", ...}` |
| `delete_rule` | `POST /actions/propose` with `{"kind": "delete_rule", ...}` |

Neither write tool changes anything itself, and neither ever calls `/admin/rules`. The
tool validates its input, then posts a proposal. The gateway stores it as a pending
action, emails the account owner one plain sentence saying what will change plus an
Approve link and a Reject link, and answers the tool with the action id. Only the
owner's click on the Approve link applies the write (through the same code path as
`POST /admin/rules`); Reject closes it; either link works once, and both stop working
ten minutes after the proposal. If the email cannot be sent, the proposal is expired on
the spot and the tool reports the failure: no email means no pending action. The tool's
reply is `Sent for approval. Check your email. Action #N expires at TIME.`; call
`get_action_status(N)` afterwards to see the outcome. A rule the account already has (same team, from and to, case-insensitive) is refused at proposal time with `This rule already exists.` and nothing is sent.

Every read returns clean text and fails gracefully: a gateway that is down yields
`slice gateway not running at <url>`, never a stack trace.

> **Note:** `/dashboard/recent` does not expose `latency_ms` (the column exists in the
> `requests` table but the route omits it). `get_recent_requests` reports the fields the
> endpoint does return; no gateway route was changed to add latency.

## Tests

```bash
python -m pytest tests/test_mcp_server.py
```

The gateway is always faked with `respx`: no live network.
