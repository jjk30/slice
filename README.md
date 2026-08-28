<p align="center">
  <img src="dashboard/public/favicon.png" alt="slice" width="120" />
</p>

<h1 align="center">slice</h1>

<p align="center">A self-hosted LLM gateway that routes, caches, and caps your AI spend, then proves the savings.</p>

<p align="center">
  <a href="https://github.com/jjk30/slice/actions/workflows/ci.yml"><img src="https://github.com/jjk30/slice/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  &nbsp;<a href="https://sliceapp.dev">sliceapp.dev</a>
</p>

## The problem

Every request to a frontier model costs money, and most of them do not need a frontier model. Teams pay top-tier prices for prompts a cheaper model would answer just as well, re-ask questions they already answered, and find out they blew the budget only when the bill lands. slice sits in front of the providers and fixes all three before the money is spent.

## What slice does

- **Route.** A small classifier judges each prompt easy or hard, and sends easy ones to a cheaper model. Hard prompts stay on the model you asked for.
- **Cache.** Identical prompts are served from Redis at zero provider cost.
- **Cap.** A monthly per-account budget blocks spend past the cap and warns before it.
- **Agent loop.** When a request is routed down, slice tries the cheap model, checks the answer, and escalates up a model ladder only if the answer is not good enough.
- **Evaluate.** A sample of routed-down answers is scored with RAGAS, out of band, so quality is measured, not assumed.
- **Alerts.** Budget warnings, budget blocks, and new high-risk cloud findings fire by email and WhatsApp.
- **AWS scanner.** A read-only scan of a connected AWS account for security risks and cost waste, with a daily Cost Explorer pull.
- **Live dashboard.** A Vue single-page app streams spend, routing, cache, and eval numbers over Server-Sent Events.
- **MCP server.** A stdio MCP server exposes spend, rules, recent requests, and eval summaries to an MCP client.
- **CLI.** A `slice` command logs you in through GitHub and prints the lines that point a tool at the gateway.
- **Mac desktop app.** An Electron app that logs in and opens the bundled dashboard against the hosted gateway.

## The number

A fixed 50-prompt developer workload was sent twice: once straight to Anthropic, once through slice, with the same baseline model (`claude-sonnet-4-6`) named in both legs.

**Same workload, 45.28% cheaper through slice: $0.258801 direct, $0.141615 through slice.**

- 27 requests were routed down to a cheaper model, saving $0.087675.
- 5 identical prompts were served from cache at $0, saving $0.029511.
- The 19 hard prompts stayed on `claude-sonnet-4-6`, the model the client asked for.

Routing plus cache reconcile exactly to the $0.117186 saved. Reproduce it with [demo/run_demo.py](demo/run_demo.py) over [demo/batch.json](demo/batch.json); the full breakdown is in [demo/results/summary.md](demo/results/summary.md).

## How it works

```
   client                              slice                              providers
 -----------      +----------------------------------------------+      ------------
  Anthropic       |  auth          budget         cache          |       Anthropic
 /v1/messages     |  slk_live_     guard          Redis          |       (your key)
      or     ----->  fail closed   upper bound    hit -> return  |  --->  OpenAI
  OpenAI          |                                              |       Google Gemini
 /v1/chat/        |  judge + router   ---->   agent loop         |       NVIDIA NIM
 completions      |  easy / hard              try, check,        |
                  |  pin > rule > auto        escalate ladder    |
                  +----------------------------------------------+
                        |             |               |
                        v             v               v
                   PostgreSQL       FAISS         RAGAS sample
                   log (fire        judge hint    ~5% of routed
                   and forget)      (auto path)   down answers
                        |
                        v
                   SSE ----> live dashboard        alerts ----> email / WhatsApp
```

1. A request enters as Anthropic Messages JSON on `/v1/messages` or OpenAI chat JSON on `/v1/chat/completions`.
2. Auth resolves the slice key to an account and fails closed on any doubt.
3. The budget guard rejects the request with a 429 if the account is over its monthly cap.
4. The cache is checked next, and a hit returns immediately with an `x-slice-cache: hit` header, never touching a provider.
5. The router picks a served model in precedence order pin, then rule, then the judge on the auto path, and the judge is free to route an easy prompt down to a cheaper model.
6. On the auto-routed-down, non-streaming path the agent loop runs, escalating up the model ladder until an answer passes or a cost ceiling is reached.
7. The chosen adapter calls the provider: Anthropic with your `x-api-key`, the others with the server key.
8. After the response is sent, logging, the dashboard SSE publish, RAGAS sampling, and Prometheus recording all happen in the background.

## Design rules

- Logging, metrics, eval, and alerts are fire and forget: they run after the response and never block or fail a request.
- Redis failures fail open: a down cache, budget counter, or rate limiter is skipped and traffic forwards as if the layer were not there.
- A judge that errors, times out, or returns anything unexpected counts as hard, so the client keeps their model.
- The agent loop runs only on the auto routing path, and only when auto-routing sent the request down to a cheaper model.
- Routing precedence is pin, then rule, then auto: pins and rules apply even when auto-routing is off.
- The budget estimate the agent loop checks against the ceiling is an upper bound, so the ceiling is never crossed.
- Auth fails closed: a missing, malformed, unknown, or revoked key is a 401, and an unreadable key store is a 503, never an open door.

## Quickstart

### Path 1: point an existing tool at the hosted gateway

Install the CLI and log in through the GitHub device flow:

```bash
pip install slice-gateway
slice login
```

Inside a clone of this repo you can run the module form instead: `python -m app.cli login`.

Login mints a slice key (`slk_live_...`) and saves it to `~/.slice/config.json`. Point Claude Code, or any Anthropic client, at slice with three variables:

```bash
export ANTHROPIC_BASE_URL=https://api.sliceapp.dev
export ANTHROPIC_API_KEY=sk-ant-api...     # your own Anthropic key
export ANTHROPIC_AUTH_TOKEN=slk_live_...   # your slice key, from slice login
```

`ANTHROPIC_AUTH_TOKEN` goes out as `Authorization: Bearer`, which is where slice reads its key. `ANTHROPIC_API_KEY` stays your own Anthropic key in `x-api-key`, and slice forwards it upstream. Claude Code will print a notice that env auth takes precedence over your claude.ai login while these are set, which is expected; unset the three variables to go back to normal.

The same two headers as raw curl:

```bash
curl https://api.sliceapp.dev/v1/messages \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-5","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

Run `slice use anthropic` (or `openai`, `curl`, `claude-code`) to print the exact lines for your tool. In a repo clone, `python -m app.cli use ...` works the same way.

### Path 2: self-host

Copy the environment template and bring up the gateway with Postgres and Redis:

```bash
cp .env.example .env
docker compose up --build
```

The gateway listens on `http://localhost:8080` and applies its own database migrations on startup. To run it on Kubernetes instead, a kind cluster and kustomize manifests live under [k8s/](k8s/):

```bash
make -C k8s kind-up kind-load secrets deploy
```

## Providers and formats

slice speaks the Anthropic Messages format internally, and every adapter translates to and from it. Two inbound formats are accepted:

- **Anthropic Messages** on `POST /v1/messages`, in and out.
- **OpenAI Chat Completions** on `POST /v1/chat/completions`, translated to Anthropic internally and back to OpenAI shape on the way out.

Requests route to one of four upstreams, picked by the model name: `claude-*` to Anthropic, `gpt-*` and o-series to OpenAI, `gemini-*` to Google Gemini, and any `vendor/model` name to NVIDIA NIM. Anthropic always uses your own `x-api-key`. OpenAI, Gemini, and NVIDIA NIM use the server-configured keys (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `NIM_API_KEY`); a request routed to an unconfigured provider gets a clean 401 and never leaves the machine.

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Gateway | FastAPI, uvicorn, httpx | Async proxy that streams responses and hangs background work off them. |
| Routing and agent | LangGraph | The pin/rule/auto router and the try/check/escalate loop are each a small state graph. |
| RAG | FAISS, sentence-transformers | Per-team index of past prompts gives the judge a semantic hint on the auto path. |
| Eval | RAGAS, LangChain | Scores a sample of routed-down answers for relevancy, out of band. |
| Safety | NeMo Guardrails | Self-check input and output rails around the agent loop. |
| Tracing | LangSmith | Optional LangChain tracing, a no-op when unset. |
| Data | PostgreSQL, Redis | Postgres logs every request and holds accounts and keys; Redis holds cache, budgets, and rate limits. |
| Dashboard | Vue 3, Vite | Single-page app fed by read endpoints and a live SSE stream. |
| Desktop | Electron | Mac app wrapping GitHub login and the bundled dashboard. |
| Alerts | Resend, Twilio | Email through Resend, WhatsApp through Twilio, both fire and forget. |
| MCP | mcp (FastMCP) | Stdio server exposing spend, rules, recent requests, and eval over HTTP to the gateway. |
| Infra | Terraform, Docker, Caddy | ECS or a single EC2 box, both behind Caddy or an ALB with TLS. |
| Monitoring | Prometheus, Grafana | `slice_*` counters and histograms scraped from `/metrics`. |
| Scanner | boto3 | Read-only AWS security and cost-waste checks plus Cost Explorer. |

## Project layout

```
app/            FastAPI gateway: router, judge, agent loop, adapters, auth, cache, budgets, eval, guardrails, scanner, alerts, dashboard API, CLI
adapters/       Provider adapters: Anthropic, OpenAI, Google Gemini, NVIDIA NIM (under app/)
mcp_server/     Stdio MCP server exposing gateway reads and rule writes
dashboard/      Vue 3 + Vite single-page dashboard
desktop/        Electron Mac app wrapping login and the dashboard
demo/           Fixed-batch cost demo: runner, prompts, results
guardrails/     NeMo Guardrails config and slice-specific rail prompts
migrations/     SQL migrations applied on startup
k8s/            kind cluster config and kustomize manifests
infra/          Terraform for AWS: ECS stack, a cheap EC2 stack, and cross-account onboarding
scripts/        Offline RAG index builder
website/        Static marketing page
tests/          Test suite
```

## Tests and CI

Run the suite with pytest:

```bash
pytest
```

The suite collects 551 tests. CI is defined in [.github/workflows/ci.yml](.github/workflows/ci.yml): on every push and pull request it spins up PostgreSQL 16 and Redis 7 as services, installs the requirements, applies the schema and migrations through the app's own startup path, and runs the full suite. There is no build-or-deploy step in CI.

## Live

- API: [api.sliceapp.dev](https://api.sliceapp.dev)
- Dashboards: [grafana.sliceapp.dev](https://grafana.sliceapp.dev)
- Site: [sliceapp.dev](https://sliceapp.dev)

## Status

Verified in production: the gateway, routing, caching, budgets, auth, the dashboard, and Prometheus and Grafana monitoring run on a single EC2 box behind Caddy with TLS. Real Claude Code has been run end to end through api.sliceapp.dev using `ANTHROPIC_AUTH_TOKEN`. The fixed-batch cost demo above is a real paired run against live providers. Users bring their own provider keys by design.

Not yet verified in production:

- WhatsApp alerts are wired through Twilio but not verified in production, because Twilio is still on a trial account.
- The Mac desktop app ships unsigned for now, so macOS Gatekeeper will warn on first open.

Coming next: a PyPI package for the CLI, a signed Mac app, and a LoRA-tuned routing judge.
