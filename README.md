# slice

**The AI cost gateway.** slice sits in front of every AI model call your team makes — routing each request to the cheapest model that works, caching repeats, and capping spend before it runs away.

Change one line in your app, and your AI bill drops.

---

## The problem

AI providers bill per token, with no ceiling. The more your engineers and apps use AI, the higher the bill climbs — and teams have no easy way to see who's spending what or to stop it. This is what blew up budgets at companies like Microsoft, Uber, and Meta in 2026: great tools, adopted fast, with usage-based billing and no controls.

slice is the meter and the valve on that pipe. It never sees your codebase — only the API traffic flowing through it.

---

## What slice does

- **Route** — sends each request to the cheapest model that can handle it.
- **Cache** — reuses answers for repeated requests instead of paying twice.
- **Cap** — enforces budgets per team, with a kill switch when limits are hit.
- **Recommend** — learns which model is best and cheapest per task, from your own usage.
- **Agentic** — a bounded loop: try a cheap model, check the result, escalate only if needed, and stop at a budget ceiling.
- **Observe** — a live dashboard of spend, savings, and team budgets, plus alerts (email, Slack, SMS, WhatsApp).

---

## How it works

```
Developer's app / Claude Code
        │  (requests)
        ▼
   slice gateway  ── Redis (cache + live spend counters)
   guard · cache · router · agent
        │                  └── Postgres (logs + cost)
        ▼                          │
   AI providers                    ▼
   Claude · GPT · Gemini · Grok   Kedro (offline) ──► model rankings ──► back to router
        ▲
        └── spend alerts ──► developer
```

A request enters the gateway, passes through the budget guard, cache, router, and agent, then goes to the cheapest provider that fits. Every request is logged to Postgres. Kedro reads those logs offline and feeds fresh model rankings back to the router, so routing gets smarter over time.

---

## How users connect

There are two sides.

**What the user does** — change one line. Point any AI tool (Claude Code, the SDK, production code) at slice and use a slice key:

```bash
export ANTHROPIC_BASE_URL=https://api.slice.dev
export ANTHROPIC_API_KEY=slk_live_your_key_here
```

The same two lines work everywhere: terminal, VS Code / Cursor (`.env`), production, and CI.

**What slice provides** so that one line works:

1. A public endpoint (`api.slice.dev`) that's always on.
2. Accounts and keys — users sign up and the dashboard mints a slice key tied to their account.
3. A doorman — on every request, slice checks the key, finds the account, checks the budget, then forwards to the cheapest provider.

slice can hold each user's real provider key (encrypted), so their key never leaves the server and access can be capped or revoked instantly.

slice is **self-hostable** — teams can run it inside their own infrastructure, so it's never a third-party dependency and their data never leaves their network.

---

## Tech stack

**Languages**
- TypeScript / JavaScript — the gateway (backend) and the Vue dashboard (frontend).
- Python — the offline data pipeline (Kedro).
- SQL — Postgres queries.
- HCL — Terraform (infrastructure as code).
- YAML — GitHub Actions and deploy config.
- Bash — scripts and Docker steps.

**Backend (gateway)**
- Node.js + Express — the proxy server.
- Redis — cache and live spend counters.
- PostgreSQL — request logs, tokens, and cost.

**Frontend (dashboard)**
- Vue 3 + Vite — the dashboard app.
- Chart.js — spend graphs.

**Data layer**
- Kedro (Python) — offline pipeline: logs → model rankings.
- pandas — data crunching.
- scikit-learn (optional) — train a model to predict the best model per task.

**AI layer**
- Provider APIs — Anthropic, OpenAI, Google, xAI.

**Alerts**
- Email (Resend / SendGrid), Slack / Teams, SMS (Twilio), WhatsApp Business API.

**Deployment**
- Docker — containerize the app.
- GitHub Actions — CI/CD (test, build, deploy on push).
- AWS ECR — image registry.
- AWS ECS Fargate — runs the container in production.
- Terraform — defines the AWS setup as code.
- Kubernetes — optional, for large-scale or learning.

**Security**
- AWS Secrets Manager / KMS — stores users' provider keys for the virtual-key system.

---

## Suggested project structure

```
slice/
├── gateway/          # Node + Express proxy (TypeScript)
│   ├── src/
│   └── package.json
├── dashboard/        # Vue 3 + Vite frontend
├── pipeline/         # Kedro project (Python) — logs → rankings
├── infra/            # Terraform (HCL) + Dockerfile
├── .github/workflows # GitHub Actions CI/CD
└── README.md
```

---

## Getting started (Phase 1 — the proxy)

The foundation is a transparent proxy that forwards every request to the provider and logs the model, status, latency, and token usage.

```bash
cd gateway
npm install
cp .env.example .env
npm start
```

Point a client at it and watch the logs:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
```

Once a request flows through, every later feature (logging to Postgres, routing, caching, budgets) plugs into this same path.

---

## Build plan

1. **Proxy** — forward requests, log to the console. *(done)*
2. **Logging** — write those logs to Postgres.
3. **Router** — pick cheap vs expensive model. First real savings.
4. **Cache + caps** — Redis cache and per-team budget limits.
5. **Dashboard** — Vue UI reading from Postgres.

**Then:**
- Recommendation registry + Kedro pipeline (data-driven model rankings).
- Bounded agent loop (try → check → escalate → stop at budget).
- Multi-provider adapters (GPT, Gemini, Grok).
- Alerts (email, Slack, SMS, WhatsApp).
- Deploy: Docker → GitHub Actions → ECR → ECS Fargate, defined in Terraform.

---

## The demo that sells it

Run a fixed batch of tasks through slice twice — once straight to the provider, once through the gateway — and show the dollar difference. That single number ("cut spend ~60% on the same workload") is the headline.
