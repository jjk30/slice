# slice

A transparent AI cost gateway that sits in front of the Anthropic API. Point your
client at slice instead of `api.anthropic.com` and it forwards every request
unchanged, streams the response straight back, and adds cost controls on top:
per-request logging, a classifier-based model **router**, a Redis response
**cache**, and per-team **budget caps**.

The client keeps sending its own Anthropic API key in the request header — slice
never stores or requires a key of its own.

## Status

| Phase | Feature | What it does |
|------:|---------|--------------|
| 1 | Transparent proxy | Forwards all paths/methods to Anthropic, streams responses (SSE-safe), logs one line per request with model, status, latency, and token usage. |
| 2 | Postgres logging | Persists every request to a `requests` table. Fire-and-forget — a DB outage never blocks the proxy. |
| 3 | Classifier router | A cheap "judge" model rates each request EASY/HARD and rewrites `model` to the cheapest tier that fits. Verdicts are cached; a judge failure falls back to the client's original model. |
| 4 | Cache + budget caps | Redis response cache (identical non-streaming requests served without calling the provider) and a source-agnostic budget engine that tracks per-team spend and blocks (429) over the cap. Both fail open if Redis is down. |

## Architecture

```
client ──▶ slice gateway ──▶ api.anthropic.com
                │
   budget check ─▶ cache lookup ─▶ route ─▶ forward ─▶ store in cache ─▶ record spend ─▶ persist row
                │                                                    │
              Redis (cache + spend counters)              Postgres (requests, budget_events)
```

All code lives in [`gateway/`](gateway/):

```
gateway/
  src/
    index.ts     # express app + bootstrap
    proxy.ts     # forward + stream tee + order-of-operations
    router.ts    # Phase 3 classifier router
    cache.ts     # Phase 4 response cache
    budget.ts    # Phase 4 source-agnostic budget engine
    pricing.ts   # per-model price table
    redis.ts     # fail-open Redis client
    db.ts        # Postgres pool + migrations
    logger.ts    # structured per-request logging
  db/init/       # Postgres schema (runs on first container start)
  docker-compose.yml
  .env.example
```

## Quick start

```bash
cd gateway
cp .env.example .env          # fill in / keep dev defaults
docker compose up -d          # Postgres + Redis
npm install && npm start      # gateway on :8080
```

Point a client at it:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
# or:
curl http://localhost:8080/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-opus-4-8","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

## Configuration

All config is read from the environment — see [`gateway/.env.example`](gateway/.env.example).
Highlights:

- `PORT`, `ANTHROPIC_UPSTREAM`
- `DATABASE_URL`, `REDIS_URL`
- `ROUTER_ENABLED`, `JUDGE_MODEL`, `ROUTER_CHEAP_MODEL`, `ROUTER_STRONG_MODEL`
- `CACHE_ENABLED`, `CACHE_TTL_SECONDS`
- `BUDGET_ENABLED`, `BUDGET_LIMIT_USD`, `BUDGET_LIMIT_<TEAM>`, `BUDGET_WARN_RATIO`

Per-request overrides: `x-slice-team` (budget account), `x-slice-cache: off`,
`x-slice-route: off`.

### Inspecting data

```bash
# recent requests (model, routing, cache, cost)
docker compose exec db psql -U slice -d slice -c \
  "select id, requested_model, routed_model, verdict, cache_hit, round(cost_usd::numeric,6) cost, status from requests order by id desc limit 10;"

# a team's running spend / recent cap events
docker compose exec redis redis-cli get "slice:spend:default"
docker compose exec db psql -U slice -d slice -c "select * from budget_events order by id desc limit 10;"
```

## Notes

- **Secrets**: the gateway needs no API key — clients pass their own. `.env` is
  gitignored; only `.env.example` (placeholders) is committed.
- **Safety properties**: Postgres, Redis cache, and budget caps all fail open —
  a backing-store outage degrades gracefully and never takes down the proxy.
- Requires Node 18+ (uses global `fetch` + Web Streams; tested on Node 24).
