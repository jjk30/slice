<p align="center">
  <a href="https://sliceapp.dev"><img src="dashboard/public/favicon.png" alt="slice" width="120" /></a>
</p>

<h1 align="center">slice</h1>

<p align="center">A self-hosted LLM gateway that routes, caches, and caps your AI spend, then proves the savings.</p>

<p align="center">
  <a href="https://github.com/jjk30/slice/actions/workflows/ci.yml"><img src="https://github.com/jjk30/slice/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  &nbsp;<a href="https://sliceapp.dev">sliceapp.dev</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/FastAPI-gateway-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/LangGraph-router%20%2B%20agent%20loop-1C3C3C?logo=langgraph&logoColor=white" alt="LangGraph" />
  <img src="https://img.shields.io/badge/LangChain-RAG-1C3C3C?logo=langchain&logoColor=white" alt="LangChain" />
  <img src="https://img.shields.io/badge/FAISS-semantic%20search-0467DF?logo=meta&logoColor=white" alt="FAISS" />
  <img src="https://img.shields.io/badge/RAGAS-eval-6E40C9" alt="RAGAS" />
  <img src="https://img.shields.io/badge/NeMo%20Guardrails-safety-76B900?logo=nvidia&logoColor=white" alt="NeMo Guardrails" />
</p>
<p align="center">
  <img src="https://img.shields.io/badge/Anthropic-Claude-D97757?logo=anthropic&logoColor=white" alt="Anthropic" />
  <img src="https://img.shields.io/badge/OpenAI-GPT-412991?logo=openai&logoColor=white" alt="OpenAI" />
  <img src="https://img.shields.io/badge/Google-Gemini-4285F4?logo=googlegemini&logoColor=white" alt="Google Gemini" />
  <img src="https://img.shields.io/badge/NVIDIA%20NIM-open%20models-76B900?logo=nvidia&logoColor=white" alt="NVIDIA NIM" />
  <img src="https://img.shields.io/badge/LoRA%20judge-Qwen2.5--0.5B-FFD21E?logo=huggingface&logoColor=black" alt="LoRA judge" />
</p>
<p align="center">
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 16" />
  <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white" alt="Redis 7" />
  <img src="https://img.shields.io/badge/Vue%203%20%2B%20Vite-SSE%20dashboard-4FC08D?logo=vuedotjs&logoColor=white" alt="Vue 3 + Vite" />
  <img src="https://img.shields.io/badge/Electron-Mac%20app-47848F?logo=electron&logoColor=white" alt="Electron" />
  <img src="https://img.shields.io/badge/MCP-server-000000?logo=modelcontextprotocol&logoColor=white" alt="MCP server" />
</p>
<p align="center">
  <img src="https://img.shields.io/badge/Docker-arm64-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/Terraform-IaC-844FBA?logo=terraform&logoColor=white" alt="Terraform" />
  <img src="https://img.shields.io/badge/AWS-EC2%20%7C%20ECR%20%7C%20S3%20%7C%20SSM-FF9900?logo=amazonwebservices&logoColor=white" alt="AWS" />
  <img src="https://img.shields.io/badge/Caddy-TLS-1F88C0?logo=caddy&logoColor=white" alt="Caddy" />
  <img src="https://img.shields.io/badge/Kubernetes-kind%20%2B%20HPA-326CE5?logo=kubernetes&logoColor=white" alt="Kubernetes" />
  <img src="https://img.shields.io/badge/Prometheus-Grafana-E6522C?logo=prometheus&logoColor=white" alt="Prometheus and Grafana" />
  <img src="https://img.shields.io/badge/GitHub%20Actions-CI-2088FF?logo=githubactions&logoColor=white" alt="GitHub Actions" />
  <img src="https://img.shields.io/badge/PyPI-slice--gateway-3775A9?logo=pypi&logoColor=white" alt="PyPI" />
</p>

## Why I built this

In 2026 the AI bill came due. One enterprise reportedly ran up about $500M on Claude in a single month with no cap (Axios). Uber's AI budget for the year was reportedly gone by April, with its heaviest Claude Code users costing around $2,000 a month each. Microsoft reportedly pulled Claude Code from a large division after six months because adoption outran the budget.

The tools were good. The brakes were missing: usage-based billing with no ceiling, and no view of where the money went.

I wanted the meter and the valve. A gateway that sits in front of every AI call, sends the easy ones to a cheaper model, answers repeats from cache, and stops spend at a number you chose. And I wanted the savings measured on a real bill, not promised on a slide.

There was a second reason. I was learning agentic AI properly, working through the NVIDIA Agentic AI Professional certification, and I did not want the coursework to end in a notebook. slice is that stack (LangGraph, RAG, evaluation, guardrails, deployment) doing a real job on real traffic.

## What slice does

- **Route.** A small classifier judges each prompt easy or hard, and sends easy ones to a cheaper model. Hard prompts stay on the model you asked for.
- **Cache.** Identical prompts are served from Redis at zero provider cost.
- **Cap.** A monthly per-account budget blocks spend past the cap and warns before it.
- **Agent loop.** When a request is routed down, slice tries the cheap model, checks the answer, and escalates up a model ladder only if the answer is not good enough.
- **Evaluate.** A sample of routed-down answers is scored with RAGAS, out of band, so quality is measured, not assumed.
- **Alerts.** Budget warnings, budget blocks, and new high-risk cloud findings fire by email. WhatsApp is wired but parked, see Status.
- **AWS scanner.** A read-only scan of a connected AWS account for security risks and cost waste, with a daily Cost Explorer pull.
- **Live dashboard.** A Vue single-page app streams spend, routing, cache, and eval numbers over Server-Sent Events.
- **MCP server.** A stdio MCP server exposes spend, rules, recent requests, and eval summaries to an MCP client.
- **CLI.** A `slice` command logs you in through GitHub and prints the lines that point a tool at the gateway.
- **Mac desktop app.** An Electron app that logs in and opens the bundled dashboard against the hosted gateway. Works, but not out yet, see Status.

## The number

A fixed 50-prompt developer workload was sent twice: once straight to Anthropic, once through slice, with the same baseline model (`claude-sonnet-4-6`) named in both legs.

**Same workload, 45.28% cheaper through slice: $0.258801 direct, $0.141615 through slice.**

- 27 requests were routed down to a cheaper model, saving $0.087675.
- 5 identical prompts were served from cache at $0, saving $0.029511.
- The 19 hard prompts stayed on `claude-sonnet-4-6`, the model the client asked for.

Routing plus cache reconcile exactly to the $0.117186 saved. Reproduce it with [demo/run_demo.py](demo/run_demo.py) over [demo/batch.json](demo/batch.json); the full breakdown is in [demo/results/summary.md](demo/results/summary.md).

## Back of the envelope

Everything in this section starts from the measured run above. Projections are marked as projections.

**Where the 45% comes from.** Routing did the heavy lifting: 27 of 50 prompts (54%) were judged easy and served by a cheaper model, which removed 34 points of the bill. Cache did the rest: 5 of 50 prompts (10%) were repeats and cost nothing, another 11 points. Per request, the direct leg averaged $0.0052 and the slice leg $0.0028.

**What it means for a team (projection).** At the same easy-to-hard mix, a team spending $8,000 a month on AI keeps about $3,600 of it. Your mix will differ. A team that only sends hard prompts saves less; a team running bulk jobs with repeats saves more.

**What the gateway costs to run.** Production is one `t4g.small` instance. At the on-demand rate at the time of writing (about $0.017 an hour) that is about $12 a month for the box, and it runs everything: gateway, Postgres, Redis, Caddy, Prometheus, Grafana, and the static site. At a 45% savings rate the box pays for itself once a team spends about $27 a month on AI. The ECS Fargate stack it replaced (a task behind a load balancer with Multi-AZ RDS) metered roughly $3 to $5 a day sitting idle, which is over $100 a month for a project between demos.

**What a routing decision costs.** Every auto-routed request needs one judge decision. A rented judge (Haiku) is a short call, a few hundred tokens in and a handful out, so a fraction of a cent each, but it scales with traffic and adds a network round trip. The LoRA judge trained in this repo makes the same decision in 133 ms with no per-call charge. It trained in 75 seconds on a free Colab T4.

**Why the cache is nearly free.** A hit is one Redis lookup and no provider call. The key hashes the full request body except stream and metadata, so two prompts only collide when they would have produced the same answer anyway.

**Why the agent loop cannot overspend.** Before each escalation it estimates the cost of the next rung as an upper bound, and it stops when that bound would cross the ceiling. The estimate being an upper bound is the whole trick: the ceiling is never crossed, at the price of occasionally stopping one rung early.

## System design

### Request path

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
                   SSE ----> live dashboard        alerts ----> email
```

1. A request enters as Anthropic Messages JSON on `/v1/messages` or OpenAI chat JSON on `/v1/chat/completions`.
2. Auth resolves the slice key to an account and fails closed on any doubt.
3. The budget guard rejects the request with a 429 if the account is over its monthly cap.
4. The cache is checked next, and a hit returns immediately with an `x-slice-cache: hit` header, never touching a provider.
5. The router picks a served model in precedence order pin, then rule, then the judge on the auto path, and the judge is free to route an easy prompt down to a cheaper model.
6. On the auto-routed-down, non-streaming path the agent loop runs, escalating up the model ladder until an answer passes or a cost ceiling is reached.
7. The chosen adapter calls the provider: Anthropic with your `x-api-key`, the others with the server key.
8. After the response is sent, logging, the dashboard SSE publish, RAGAS sampling, and Prometheus recording all happen in the background.

### The three routing layers

Routing precedence is fixed: **pin, then rule, then auto.**

- A **pin** is a per-account instruction to always serve a given model. It wins over everything.
- A **rule** is a team switch rule, for example "route lint fixes to Haiku". Rules are enforced even when auto-routing is off.
- **Auto** is the judge. It reads the prompt, plus a semantic hint from FAISS (the nearest past prompts for that team and what they were routed to), and answers easy or hard. Easy goes down the ladder, hard stays on the requested model.

The judge is a component with a contract, not a specific model. Production uses Haiku. A NIM open model plugs in by config. The LoRA judge in [colab/](colab/) is the third option: a Qwen2.5-0.5B fine-tune trained on 728 labeled rows of what the live router actually did (90/10 split), scoring 92% on 100 unseen prompts against a 54% majority baseline, at 133 ms per decision. In a paired RAGAS comparison against the live router it disagreed on 8 answers, with answer relevancy of 0.89. It is benchmarked, not deployed.

### The agent loop

Only auto-routed-down, non-streaming requests enter the loop. It tries the cheap model, checks the answer (truncated answers count as failures), and escalates one rung up the cross-provider ladder if the check fails. A dead provider is skipped and the ladder continues. Before each rung it compares an upper-bound cost estimate against the ceiling and stops if the next rung would cross it. NeMo Guardrails wraps the loop with self-check rails on input and output.

### Data and background work

- **PostgreSQL** is the record book: every request with tokens and cost, accounts, hashed keys, rules, eval scores, scanner findings. Logging is fire and forget.
- **Redis** is the fast path: response cache, live monthly spend counters, and per-account rate limits. All of it fails open.
- **FAISS** holds past prompts as vectors, one index per team, built offline by [scripts/](scripts/) and read by the judge on the auto path.
- **RAGAS** scores about 5% of routed-down answers out of band. A RAGAS failure never touches a request.
- **LangSmith** traces agent steps when a key is set and is a no-op otherwise.
- **Prometheus** scrapes `slice_*` counters and histograms from `/metrics`; **Grafana** shows them.
- **SSE** pushes to the dashboard the moment a request finishes. No polling.

### Security model

- **Login** is the GitHub device flow: no password ever typed into a terminal, nothing stored but the GitHub identity.
- **Slice keys** (`slk_live_...`) are stored as SHA-256 hashes, tied to one account, revocable from the dashboard.
- **Provider keys**: Anthropic always uses the caller's own `x-api-key`, forwarded and never stored. OpenAI, Gemini, and NIM use server keys held in AWS Secrets Manager.
- **Fail closed on auth, fail open on availability.** A bad, missing, or revoked key is a 401 and an unreadable key store is a 503. A down Redis is skipped and traffic flows.
- **Writes need auth**, both on the API and through MCP, where write tools require a confirmation step.
- **The AWS scanner** uses a read-only IAM role created by a one-click CloudFormation template in the user's account, and only ever reads.

### Where it runs

```
                   Route 53                    GitHub Actions (CI: pytest, Postgres 16, Redis 7)
                      |                        Docker buildx (linux/arm64) -> ECR
   sliceapp.dev  api.sliceapp.dev  grafana.sliceapp.dev
                      |
        +-------------v----------------------------------+
        |  EC2 t4g.small (arm64), reached over SSM, no SSH |
        |                                                   |
        |  Caddy (TLS, static site, reverse proxy)          |
        |    -> gateway (FastAPI)  -> Postgres  -> Redis    |
        |    -> Grafana            <- Prometheus            |
        +---------------------------------------------------+
                      |
              nightly Postgres backups -> S3
              secrets -> AWS Secrets Manager
```

One box, one `docker compose` file, all of it defined in Terraform under [infra/](infra/). Deploys are deliberate: build the arm64 image, push to ECR, pull on the box over SSM, `docker compose up -d gateway`. CI runs the full test suite on every push but does not deploy; a green suite is a gate, not a trigger.

The same image runs on Kubernetes. [k8s/](k8s/) holds a kind cluster config and kustomize manifests with a horizontal pod autoscaler, and under load it scaled from 2 to 4 replicas. That is the demo path for the orchestrated setup; the single box is the live path, because a `t4g.small` does not need an orchestrator.

## Tools, and why each one

| Layer | Tool | Why this one |
|---|---|---|
| Gateway | FastAPI, uvicorn, httpx | Async proxy that streams responses and hangs background work off them. Python, so the agent stack lives in the same process. |
| Routing and agent | LangGraph | The pin/rule/auto router and the try/check/escalate loop are each a small state graph. LangGraph runs the steps; the models do the thinking. |
| RAG | FAISS, sentence-transformers | Per-team index of past prompts gives the judge a semantic hint. Free, local, and the right size for this data. |
| Judge | Haiku, NIM open models, LoRA Qwen2.5-0.5B | Swappable by config. Haiku in production, the LoRA judge benchmarked against it. |
| Eval | RAGAS, LangChain | Scores a sample of routed-down answers for relevancy, out of band, and benchmarks judges against each other. |
| Safety | NeMo Guardrails | Self-check input and output rails around the agent loop. |
| Tracing | LangSmith | Optional LangChain tracing, a no-op when unset. |
| Data | PostgreSQL, Redis | Postgres logs every request and holds accounts and keys; Redis holds cache, budgets, and rate limits. |
| Providers | Anthropic, OpenAI, Google Gemini, NVIDIA NIM | Anthropic is the wire format; the others translate. NIM adds open models with free credits. |
| Dashboard | Vue 3, Vite, Chart.js | Single-page app fed by read endpoints and a live SSE stream. |
| Desktop | Electron | Mac app wrapping GitHub login and the bundled dashboard. |
| Alerts | Resend, Twilio | Email through Resend, WhatsApp through Twilio, both fire and forget. |
| MCP | mcp (FastMCP) | Stdio server exposing spend, rules, recent requests, and eval over HTTP to the gateway. |
| Fine-tuning | Hugging Face PEFT on a Colab T4 | Own judge trained on own logs, free GPU, honest benchmark. |
| Infra | Terraform, Docker, Caddy, ECR, SSM | One EC2 box behind Caddy with TLS; the ECS stack kept in Terraform for demos. |
| Monitoring | Prometheus, Grafana | `slice_*` counters and histograms scraped from `/metrics`. |
| Orchestration | Kubernetes (kind, kustomize) | Manifests plus HPA, proven locally under load. |
| Scanner | boto3, CloudFormation | Read-only AWS security and cost-waste checks plus Cost Explorer, through a one-click role. |
| CI | GitHub Actions | Full suite against real Postgres and Redis services on every push. |

## What I tried and set aside

Every one of these worked or was a real option. They were set aside for a reason, and each has a way back.

| Decision | What was tried | Why it was set aside | Way back |
|---|---|---|---|
| ECS Fargate, ALB, Multi-AZ RDS | Deployed and verified on the live domain. | About $3 to $5 a day idle. Wrong price for a project that is demoed, not hammered. | The Terraform is still in [infra/](infra/) in its own state. `terraform apply` brings it up in about 20 minutes for a demo; `terraform destroy` puts the meter back to zero. |
| Node and TypeScript (v1) | Eight verified phases, including cross-provider routing and team rules. | The agentic stack I was learning is Python: LangGraph, LangChain, RAGAS. Rebuilding beat translating. | The v2 gateway kept every v1 lesson: per-request cost as a first-class field, the upper-bound budget estimate, rules above the auto-routing gate. |
| Haiku as the only judge | Still the production judge. | Renting a decision that my own logs can teach a 0.5B model to make in 133 ms. | The LoRA judge is benchmarked; swapping it in is a config change plus a serving step. |
| Pinecone, pgvector | Considered for the RAG index. | Paid and oversized for this data (Pinecone); one more thing on the database (pgvector). FAISS matches the course material and is free. | Both are a drop-in behind the same retrieval call. |
| WebSockets for the dashboard | Considered. | The dashboard only listens. SSE is simpler and enough. | Not needed. |
| S3 and CloudFront for the site | Planned. | The box was already running Caddy. Zero extra cost, one place to look. | The static site is one folder; a bucket and a distribution would host it unchanged. |
| Azure AI Foundry for fine-tuning | Considered. | A managed button. A free Colab T4 with PEFT shows the actual work. | Nothing to bring back. |
| TensorRT-LLM for the judge | Explored on Lightning AI, $0 spent. | Version pinning on a T4 (0.15.0 is the last with Turing and Qwen2 support) turned a garnish into a project. | The merged judge weights are in S3 and the pin is known. A fresh session away. |
| Kubernetes in production | Manifests, kind cluster, HPA verified under load. | The live path is one box. Running an orchestrator for one replica proves nothing. | The manifests deploy the same image; EKS is a cluster away. |
| Firebase Auth | Considered as a hosted login. | slice already has GitHub device-flow login and hashed keys. Two auth systems is one too many. | If Google or email login is ever wanted, it goes in as a layer over the existing accounts. |
| WhatsApp alerts | Code complete over Twilio, one-way and two-way. | Twilio's trial blocks live delivery, and a real sender needs Meta business verification. | Upgrade Twilio, or register a WhatsApp Business sender, and it is live. |
| NeMo Agent Toolkit, Gradio, Grok adapter | Looked at. | NAT is a wrapper for later; Gradio overlaps the Vue dashboard; Grok copies the OpenAI format and is minutes to add when someone needs it. | Parked. |

## Lessons that cost me a night

- **`mcp==2.0.0` was a typosquat.** It pulled fake packages and a renamed `FastMCP`. The real package is `mcp>=1.9,<2` and it exposes `mcp.server.fastmcp.FastMCP`. Verify installs with `pip show`, not with a push that happens to go green.
- **A cache key that hashes three fields is not a cache key.** The first version hashed only model, messages, and max_tokens, so two requests with different system prompts could collide. The key now hashes the full body minus stream and metadata.
- **A config folder that is not in the Dockerfile does not exist in production.** Guardrails ran in tests and silently did nothing on the box until `COPY guardrails/ ./guardrails/` went in. The fix was verified by a log line, not a green suite.
- **Tests must not know what month it is.** A test that hardcoded an August date broke at midnight on September 1. It now anchors to the first of the current month on the same UTC clock the route uses.
- **The budget estimate must be an upper bound.** Learned in v1, kept in v2. An average estimate crosses the ceiling on the request that matters.
- **iCloud "optimize storage" will evict your virtual environment.** Six times. The repo lives outside the synced folders now.

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
  -H "anthropic-version: 2023-06-01" \
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

## Project layout

```
app/            FastAPI gateway: router, judge, agent loop, adapters, auth, cache, budgets, eval, guardrails, scanner, alerts, dashboard API, CLI
adapters/       Provider adapters: Anthropic, OpenAI, Google Gemini, NVIDIA NIM (under app/)
mcp_server/     Stdio MCP server exposing gateway reads and rule writes
dashboard/      Vue 3 + Vite single-page dashboard
desktop/        Electron Mac app wrapping login and the dashboard
demo/           Fixed-batch cost demo: runner, prompts, results
colab/          LoRA judge: training notebook, RAGAS comparison against the live router
guardrails/     NeMo Guardrails config and slice-specific rail prompts
migrations/     SQL migrations applied on startup
k8s/            kind cluster config and kustomize manifests
infra/          Terraform for AWS: ECS stack, the EC2 stack that is live, and cross-account onboarding
scripts/        Offline RAG index builder and judge training data prep
website/        Static marketing page
tests/          Test suite
```

## Tests and CI

Run the suite with pytest:

```bash
python -m pytest -q
```

The suite collects 608 tests. CI is defined in [.github/workflows/ci.yml](.github/workflows/ci.yml): on every push and pull request it spins up PostgreSQL 16 and Redis 7 as services, installs the requirements, applies the schema and migrations through the app's own startup path, and runs the full suite. There is no build-or-deploy step in CI.

## Live

- API: [api.sliceapp.dev](https://api.sliceapp.dev)
- Dashboards: [grafana.sliceapp.dev](https://grafana.sliceapp.dev)
- Site: [sliceapp.dev](https://sliceapp.dev)
- CLI: [slice-gateway on PyPI](https://pypi.org/project/slice-gateway/)

## Status

Verified in production: the gateway, routing, caching, budgets, auth, the dashboard, and Prometheus and Grafana monitoring run on a single EC2 box behind Caddy with TLS. Real Claude Code has been run end to end through api.sliceapp.dev using `ANTHROPIC_AUTH_TOKEN`. The fixed-batch cost demo above is a real paired run against live providers. Users bring their own provider keys by design.

Benchmarked, not deployed: the LoRA routing judge. Numbers above; the production judge is still Haiku.

Not yet verified in production:

- WhatsApp alerts are wired through Twilio but not verified in production, because Twilio is still on a trial account.
- The Mac app works and I can demo it, but it is not out yet. Apple wants the app signed, and my Apple developer account is waiting on approval. Once it clears, the download link goes up. Ask me and I will show it running.

Coming next: the LoRA judge serving live, a signed Mac app, and Slack alerts.

## PS

The name came first. I was craving a slice of cake when the idea landed, and the cake stayed: it is the logo, the favicon, and the thing the knife cuts on the front page. Click it at the top of this file and it takes you to [sliceapp.dev](https://sliceapp.dev).
