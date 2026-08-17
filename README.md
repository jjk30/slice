# slice

slice is an AI cost gateway. Your apps point at slice instead of straight at the AI provider, and slice cuts the bill. It routes easy requests to cheaper models, caches repeats, caps spend per team, rate limits abuse, and shows everything on a live dashboard.

This is v2: a full rebuild in Python around agents, RAG, and evaluation. v1 (Node and TypeScript) proved the idea on real traffic. v2 rebuilds the brain.

PS, I got the idea while craving a slice of cake. That is why the logo is a cake.

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
