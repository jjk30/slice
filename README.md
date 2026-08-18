# slice

slice is an AI cost gateway. Your apps point at slice instead of straight at the AI provider, and slice cuts the bill. It routes easy requests to cheaper models, caches repeats, caps spend per team, rate limits abuse, and shows everything on a live dashboard.

This is v2: a full rebuild in Python around agents, RAG, and evaluation. v1 (Node and TypeScript) proved the idea on real traffic. v2 rebuilds the brain.

PS, I got the idea while craving a slice of cake. That is why the logo is a cake lol.

## The problem

AI is billed by usage with no ceiling. In 2026 the bills came due: Uber's yearly AI budget was gone by April, and Microsoft pulled an AI coding tool from a whole division. The tools were great. The brakes were missing. Nobody could see who was spending what or stop it in time.

slice is the meter and the brake on that pipe. It never reads your code, only the API traffic passing through it, and it can run entirely inside your own network.

## What slice does

**Routes.** Each request is judged easy or hard, then sent to the cheapest model that fits. Pins and per-team switch rules always beat the auto pick.

**Caches.** A repeated request gets the answer slice already has. Free.

**Caps.** Budgets per team with warn and block. Rate limits stop abuse.

**Remembers.** Past traffic is embedded into a search index, so routing picks are based on what actually worked for your team before.

**Escalates carefully.** An agent loop tries a cheap model, checks the answer, and steps up only if it has to. It always stops at a budget ceiling.

**Grades itself.** A sample of the cheap answers is scored, so slice knows when cheap was good enough.

**Tells you.** A live dashboard, warnings by email and WhatsApp, a WhatsApp assistant you can ask "what happened?", and slice as tools inside Claude Code.

## How it works

```
your app · Claude Code · Codex
            │
            ▼
     slice gateway (FastAPI)
   guard ▸ cache ▸ router ▸ agent loop
       │               │
     Redis          Postgres ──▶ FAISS index (past usage, searchable)
            │
            ▼
   Claude · GPT · Gemini · NVIDIA NIM
            │
      alerts ──▶ email · WhatsApp
```

LangGraph runs the steps of the router and the agent loop. The models (Haiku, Nemotron, and friends) do the thinking. LangGraph is the skeleton, not the brain.

## How you connect

The end goal is a one-command setup from the terminal:

```bash
slice login            # GitHub sign-in, right from the terminal
slice init             # creates your account and a slice API key
slice use claude-code  # points Claude Code at slice
```

Until the CLI lands (phase 12), it is one line:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
```

slice speaks Anthropic format natively and also serves an OpenAI-compatible endpoint, so tools like Codex can point at it too.

## Switch rules API (local only, no auth yet)

The router applies per-team switch rules ahead of the auto judge. Manage them over a small admin API:

```bash
curl localhost:8080/admin/rules
curl -X POST localhost:8080/admin/rules \
  -H 'content-type: application/json' \
  -d '{"team":"acme","from_model":"claude-opus-5","to_model":"claude-haiku-4-5-20251001"}'
curl -X DELETE localhost:8080/admin/rules/1
```

> **Warning — these endpoints are unauthenticated.** Auth lands in a later phase (12). Until then, `/admin/*` is intended for local use only; do not expose it on a public interface. Writes persist to Postgres and refresh the in-memory rules cache immediately.

Routing is applied on the native `/v1/messages` endpoint only. The OpenAI-compatible `/v1/chat/completions` endpoint is still a straight pass-through — it is not wired to the router yet.

## Evaluation (local only, no auth yet)

slice grades a sample of the answers it served cheaper than asked. When a request is routed down (or the agent loop passes on a cheap rung), a fraction of those responses — `EVAL_SAMPLE_RATE`, default `0.05` — are scored with RAGAS in a detached background task: answer relevancy against the prompt, and, when RAG neighbors rode along, their context relevance. Scoring is fire-and-forget, exactly like the Postgres request logger — it is never awaited on the request path, every failure is swallowed, and `EVAL_SAMPLE_RATE=0` turns it off entirely without importing anything or touching startup. Scores land in the `eval_scores` table.

```bash
curl localhost:8080/admin/eval/summary
```

Returns the overall pass rate plus a breakdown per model and per `routed_from → model` pair, each with counts.

> **Warning — `/admin/eval/summary` is unauthenticated**, the same as the rules endpoints. Auth lands in a later phase (12); until then keep `/admin/*` local only.

**Tracing.** LangSmith is wired across the router and agent loop through LangChain's standard env vars (`LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`, default project `slice`). With tracing off or no key, LangChain no-ops and slice runs exactly as before — no code path requires LangSmith.

**A dependency note.** RAGAS (`ragas==0.4.3`) resolves cleanly against the installed `langchain-core` 1.5.5 / `langgraph` 1.2.11 — it does not force either up or down. It does hard-import one `langchain-community` module that the sunset community package has dropped; slice registers a tiny stub for it at import time (it never uses Vertex) rather than dragging langchain-core *down* to a version that still ships it, which would break the phase 5–7 router and agent loop. No pins are changed to accommodate RAGAS.

## Guardrails (local only, no auth yet)

slice wraps the agent loop — and **only** the agent loop — in two NeMo Guardrails self-check rails. Plain proxy traffic and the router path never touch them; the rails run exactly where the loop runs (the non-streaming, auto-routed-down path).

- **Input rail**, before the loop starts: self-checks the user prompt for attempts to manipulate slice itself — injection aimed at the routing judge or the loop's checker, attempts to force a pass on a bad answer or force an escalation, or attempts to extract slice's config or internal prompts. A block returns a clean Anthropic-shaped **400** with header `x-slice-guardrail: input`, and the request never reaches a provider.
- **Output rail**, after the loop finishes: self-checks the assembled final answer for leaked slice internals (config values, key names, internal prompts). A block returns **200** with an Anthropic-shaped standard refusal and header `x-slice-guardrail: output`. The loop's real spend is still billed; the refusal is never cached.

Only the two built-in self-check rails are used, with slice-specific prompts, configured in `guardrails/` (`config.yml` plus `prompts.yml`). No NeMo feature that needs embeddings or downloads a model at runtime is enabled — the engine constructs fully offline. The rails LLM is `GUARDRAILS_MODEL` (default the router judge model), wired through `langchain-anthropic`.

Everything fails open. `GUARDRAILS_ENABLED` (default `true`) is the whole kill switch: false means zero rails code runs, the loop behaves exactly as phase 7, and — because `nemoguardrails` is imported lazily — the server starts even if the package is broken or absent. Any exception or timeout (`GUARDRAILS_TIMEOUT_SECONDS`, default `5`) inside the rails engine is caught, logged, and treated as a pass, so a rails failure never blocks or crashes a request. Every block or fail-open error is written to the `guardrail_events` table (fire-and-forget; a down database never blocks or raises) and emitted as one structured log line.

```bash
curl localhost:8080/admin/guardrails/summary
```

Returns counts per rail and per action (`blocked` / `error`) plus the most recent events.

> **Warning — `/admin/guardrails/summary` is unauthenticated**, the same as the rules and eval endpoints. Auth lands in a later phase (12); until then keep `/admin/*` local only.

**A dependency note.** `nemoguardrails==0.23.0` (the latest) was chosen by checking its declared dependencies against the existing pins *before* installing, because nemoguardrails is known for strict langchain pins. 0.23.0 declares no `langchain`/`langchain-core`/`langchain-community` constraint at all and no `fastapi`/`starlette`/`uvicorn` core constraint, so it forces none of the phase 5–8 pins up or down — a resolver dry-run confirmed langchain 1.3.15, langchain-core 1.5.5, langchain-community 0.4.2, langgraph 1.2.11, langchain-anthropic 1.5.6, langsmith 0.11.0, and ragas 0.4.3 all stay put. The one pre-existing package it moves is `pandas` (3.0.5 → 2.3.3, its `pandas<3` cap); pandas is transitive, not a named pin, and ragas requires it only under an optional extra, so ragas is unaffected.

## Dashboard (local only, no auth yet)

One page, live. Spend and savings this month, per-team budgets with an honest tokens-remaining estimate, spend per model, the latest calls as they happen, and guardrail blocks. Vue 3 + Vite in plain JavaScript, Chart.js for the one chart, green accent. Every number on it comes from the API, which reads Postgres and Redis — nothing is hardcoded, and thin or empty data renders as zeros and dashes, never as placeholder numbers.

**Backend.** A read-only router under `/dashboard`, plus one Server-Sent Events stream:

| Endpoint | What it returns (current UTC month unless noted) |
|---|---|
| `GET /dashboard/summary` | total spend, total requests, cache hits, routed count (every request the router swapped — the auto router only routes down; a switch rule can point anywhere and its cost effect shows in savings), total savings, `unpriced_requests` (served requests with no known price, excluded from spend), eval pass rate (the phase-8 summary logic), guardrail block count (the phase-9 logic) |
| `GET /dashboard/models` | per-model request count and spend (plus `unpriced_requests`) |
| `GET /dashboard/teams` | per team: `spend_usd` (the record book, Postgres), `budget_usd` (config), `gate_spend_usd` (the live Redis counter the budget gate blocks on — it also carries the judge's cost), `budget_used_usd` / `budget_source` (the gate counter when Redis is up, the recorded spend when it is down — fail open), `remaining_usd` (cap minus budget used, floored at 0) and `estimated_tokens_remaining` |
| `GET /dashboard/recent?limit=20` | the latest requests: time, team, model, `routed_from`, status, cost, cached (any month; `limit` clamped to 1–200) |
| `GET /dashboard/events` | SSE: one `request` event per completed gateway request — `request_id`, team, model, routed_from, status, cost, cached, created_at |

**Savings, defined honestly.** For each status-200 request the router swapped (`routed_from` set), savings = what the requested model would have charged for the same input and output tokens, priced from the same table with dated-snapshot resolution, minus what was actually paid. If the `routed_from` model has no price, that row contributes 0 — never a guess. A rule that routes *up* nets negative. Cache hits are counted separately, not folded into savings.

**Tokens remaining, estimated honestly.** `estimated_tokens_remaining` = dollars remaining ÷ the team's blended cost per token this month (total cost ÷ total tokens over that team's status-200, priced requests — cache hits count, at cost 0, so the rate reflects the team's real cache hit rate; unpriced requests are left out of both sums). No traffic, zero tokens, or a zero rate gives `null` — the dashboard shows a dash, never a divide-by-zero and never a guess.

**Cheap to serve.** The month's rows are reduced in SQL (`GROUP BY` team, model, status, cached, routed_from, cost-known, with tokens and cost summed and a count per group), so a refresh costs a few hundred rows, not a table scan decoded on the gateway's event loop. Every dashboard formula is linear, so the same pure functions run over grouped rows in production and over plain seeded rows in the tests.

**Live, without polling.** Every completed request — including cache hits, and streams once they close — publishes one event to an in-process broadcaster, right next to the fire-and-forget Postgres log write. Each SSE client gets its own bounded queue (~100 events); publishing is synchronous, never blocks, and never waits on a slow client — a full queue drops its oldest event. A dashboard client connecting, hanging, or vanishing cannot slow the request path; a disconnected client's queue is dropped. The page connects with `EventSource` and reconnects on its own with a small backoff.

**Failure shapes.** Postgres down → every dashboard read endpoint returns a clean JSON `503` (`{"error": {"message": ...}}`) and the gateway's request path is untouched. Redis down → `/dashboard/teams` still returns spend from Postgres and the cap from config; only `gate_spend_usd` goes `null`.

**Running it.** Needs Node 22 (or 20.19+) and npm.

```bash
cd dashboard
cp .env.example .env      # VITE_API_BASE_URL=http://localhost:8080
npm install
npm run dev               # Vite on http://localhost:5173, talks to the gateway over CORS
npm run build             # writes dashboard/dist; the gateway serves it at http://localhost:8080/
```

In dev, the gateway allows the Vite origin through `CORS_ORIGINS` (default `http://localhost:5173`). Once built, `dashboard/dist` is served by the gateway itself at `/` — one process, no CORS needed (leave `VITE_API_BASE_URL` empty at build time to use same-origin URLs). The gateway checks for `dashboard/dist` once, at startup, so (re)start it after the first build.

> **Warning — `/dashboard/*` is unauthenticated**, exactly like `/admin/*`. Auth lands in a later phase (12); until then keep the gateway, the dashboard, and `CORS_ORIGINS` local only.

## Alerts (email via Resend)

When a team crosses its budget warn line (`warn`, the first time its month's spend reaches `BUDGET_WARN_RATIO` of the cap) or hits its cap (`block`), slice emails someone. Both moments were already detected by the Redis layer — the once-per-month SETNX latch in `add_cost` and the blocked decision in `check_budget` — and the alert fires from those exact spots as a detached `asyncio.create_task`: never awaited by the request, every exception inside caught and logged. Routing, caching, and the request path are unchanged.

- **Cooldown.** One alert per team per kind per `ALERT_COOLDOWN_SECONDS` (default 3600), latched by the Redis key `alert:cooldown:{team}:{kind}`. Inside the window nothing is sent and the attempt is recorded as `skipped_cooldown`. Redis down fails open: send anyway, never crash.
- **Channels.** A tiny interface in `app/alerts/channels.py` (`name` + `async send(alert) -> DeliveryResult`); one implementation for now, `ResendEmailChannel` — a single POST to `https://api.resend.com/emails`, Bearer `RESEND_API_KEY`, 10s timeout, plain-text body with team, kind, spend so far, cap, timestamp. Non-2xx or any exception is recorded as `failed`, never raised. Slack and WhatsApp land as new classes there; the engine doesn't change.
- **Storage.** One row per attempt in the `alerts` table (migration 009): `ts, team, kind (warn|block), channel, status (sent|failed|skipped_cooldown), detail`, written fire-and-forget like request logging.

`ALERTS_ENABLED` defaults to true only when `RESEND_API_KEY` is set (otherwise no alert code runs on any path); `ALERT_FROM` defaults to `onboarding@resend.dev`, `ALERT_EMAIL_TO` is the recipient list. See `.env.example`.

```bash
curl localhost:8080/admin/alerts/summary
```

Returns counts by kind and by status (plus the kind × status cross count) and the 10 most recent attempts.

> **Warning — `/admin/alerts/summary` is unauthenticated**, the same as the other `/admin/*` endpoints. Keep it local until auth lands (phase 12).

## Tech stack

**Backend.** Python, FastAPI, httpx. LangGraph for the router and agent loop. LangChain for the RAG pieces.

**Data.** PostgreSQL is the record book: every request, tokens, cost. Redis is the fast memory: cache, live spend counters, rate limits. FAISS is the search memory: past usage as vectors. pandas for crunching.

**AI providers.** Anthropic, OpenAI, Google Gemini, NVIDIA NIM. The routing judge starts as Haiku and is swappable by config.

**Quality and safety.** RAGAS grades the cheap answers and the retrieval. LangSmith traces every agent step. NeMo Guardrails wraps the agent and the assistants.

**Dashboard.** Vue 3 with Vite, in JavaScript. Chart.js for graphs. Live over SSE, no polling.

**Alerts and assistants.** Resend for email. Twilio for WhatsApp, one-way warnings first, then a two-way assistant that answers from your real data and applies a fix only after a confirmed yes. An MCP server so slice works inside Claude Code, reads free, writes need confirms.

**Security.** GitHub device-flow login with a small Python CLI. slice API keys for the gateway, JWT for dashboard sessions, all revocable. Fail open for availability, fail closed for auth.

**Deploy.** Docker, GitHub Actions, AWS ECR, ECS Fargate, Terraform. Prometheus and Grafana for ops. Kubernetes last.

**AWS scanner.** A one-click read-only role via CloudFormation. boto3 flags mistakes like public S3 buckets and open security groups. Cost Explorer pulls your AWS bill daily to sit next to your AI spend.

## Build plan

0 of 19 phases built. Fresh start. v1 proved the first five once already.

**Core**

1. **Proxy.** FastAPI forwards to Anthropic, streams, logs one line per request.
2. **Postgres logging.** Every request saved with tokens and cost. Never blocks traffic.
3. **Four adapters.** OpenAI, Gemini, and NIM join Anthropic behind one interface, plus an OpenAI-compatible inbound endpoint.
4. **Redis layer.** Response cache, budget caps with warn and block, rate limits. All fail open.

**Agent brain**

5. **Router.** A LangGraph graph judges easy or hard and picks the cheapest fit. Pin beats rule beats auto.
6. **RAG engine.** Past logs embedded into FAISS. Retrieval feeds the pick.
7. **Agent loop.** Try cheap, check, escalate only if needed, stop at the budget ceiling.
8. **Evaluation.** RAGAS samples the routed-down answers and stores a pass rate. LangSmith tracing everywhere.
9. **Guardrails.** NeMo rules around the agent.

**Product**

10. **Dashboard.** Vue points at the new API, live over SSE.
11. **Alerts.** Email first, WhatsApp warnings second.

**Identity and assistants**

12. **Auth.** GitHub device-flow login, the slice CLI, keys and sessions.
13. **WhatsApp assistant.** Two-way, answers from your real data, applies a fix only after a confirmed yes.
14. **MCP server.** slice inside Claude Code.

**Production**

15. **Docker and CI.** Containerize, GitHub Actions, images to ECR.
16. **AWS deploy.** Networking and ECS Fargate in Terraform, real domain and HTTPS.
17. **Ops monitoring.** Prometheus and Grafana on the live deploy.
18. **AWS scanner.** Read-only role, mistake flags, the AWS bill on the dashboard.
19. **Kubernetes.** 

## The demo that sells it

Run a fixed batch of tasks twice, once straight to the provider and once through slice, and show the dollar difference. One number: same workload, X% cheaper through slice.

## Worth knowing

You will never find a real key in this repo. The `.env` file stays private, and the public `.env.example` holds fake values only.

If Redis or Postgres goes down, your AI traffic keeps flowing. slice notes the problem and carries on.

slice can run entirely on your own machines, so your data never has to leave your network.
