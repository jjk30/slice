import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
PORT = int(os.getenv("PORT", "8080"))

# --- Redis layer (phase 4): cache, budget caps, rate limits. ---
# Unset or unreachable Redis makes every check fail open — the proxy still
# serves traffic, it just stops caching and enforcing caps until Redis returns.
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379"

# Per-team requests allowed in a rolling 60s window.
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

# Per-team monthly spend ceiling and the fraction of it that trips a warning.
BUDGET_MONTHLY_USD = Decimal(os.getenv("BUDGET_MONTHLY_USD", "25"))
BUDGET_WARN_RATIO = float(os.getenv("BUDGET_WARN_RATIO", "0.8"))

# How long a cached non-streaming response body stays warm.
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))

# Unset means request logging is disabled; the proxy still serves traffic.
DATABASE_URL = os.getenv("DATABASE_URL") or None

# Server-side provider keys. Anthropic is the exception: it always uses the
# client's own x-api-key, so it has no server key here. A None here means the
# provider is unconfigured; a request that routes to it gets a clean 401 and
# never leaves the machine.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or None
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

# NVIDIA NIM is OpenAI-compatible; only the base URL and key differ.
NIM_API_KEY = os.getenv("NIM_API_KEY") or None
NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Router (phase 5): pin ▸ rule ▸ auto, all fail open. ---
# Auto-routing is the only stage a flag can turn off; pins and per-team switch
# rules always apply. With auto off, requests forward as asked unless a rule fires.
AUTO_ROUTE_ENABLED = _bool("AUTO_ROUTE_ENABLED", True)

# Where an "easy" verdict routes to. "hard" always keeps the client's model.
ROUTE_EASY_MODEL = os.getenv("ROUTE_EASY_MODEL", "claude-haiku-4-5-20251001")

# The classifier model, and the ceiling on how long slice waits for its verdict.
# Any failure, timeout, or unexpected output counts as "hard" — never an error to
# the client. The judge only ever sees the last user message, truncated.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-haiku-4-5-20251001")
JUDGE_TIMEOUT_SECONDS = float(os.getenv("JUDGE_TIMEOUT_SECONDS", "3"))
JUDGE_MAX_INPUT_CHARS = int(os.getenv("JUDGE_MAX_INPUT_CHARS", "2000"))

# How often the in-memory switch-rules cache reloads from Postgres. Writes refresh
# it immediately; this is just the background staleness bound.
RULES_REFRESH_SECONDS = float(os.getenv("RULES_REFRESH_SECONDS", "30"))

# --- RAG retrieval (phase 6): semantic hint for the judge. ---
# Retrieval runs only on the auto path (no pin, no rule) and only feeds the judge a
# soft hint — never a hard rule. Off, or a missing index, leaves phase-5 behavior
# untouched. The index is per-team: RAG_INDEX_DIR holds one subdirectory per team
# (rag_store/<team>/), each with its own FAISS index and sidecar, so one team's
# history never feeds another team's hint. Built offline by build_rag_index.py.
RAG_ENABLED = _bool("RAG_ENABLED", True)
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
RAG_INDEX_DIR = os.getenv("RAG_INDEX_DIR", "rag_store")

# Prompt text is only logged when this is on — prompts can be sensitive, so storing
# them for the offline index build is opt-in. Off means prompt_text is never stored
# and (until it is turned on and an index is built) retrieval simply finds nothing.
RAG_STORE_PROMPTS = _bool("RAG_STORE_PROMPTS", False)

# The local sentence-transformers model used for embeddings, both at build time and
# at query time. Configurable so the model name isn't hardcoded; the two must match.
RAG_EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "all-MiniLM-L6-v2")

# --- Agent loop (phase 7): try cheap, check, escalate, stop at a ceiling. ---
# The loop extends the auto path only: it fires when auto-routing sent a request
# down to a cheaper model, the request is non-streaming, and this flag is on. Pin,
# rule, cache-hit, and gate-reject paths never loop. Every stage fails open.
AGENT_ENABLED = _bool("AGENT_ENABLED", True)

# The escalation ladder, cheap to strong, spanning providers. The loop's first try
# is always the routed cheap model; escalation walks up this ladder from the rung
# above it (or straight to the final rung if the cheap model isn't listed). The
# client's originally requested model is always the final rung, appended if absent —
# the last resort is giving them exactly what they asked for. No model names are
# hardcoded outside this default; every rung must be priceable for its cost estimate
# to be finite (an unknown price makes the estimate infinite and blocks the rung).
AGENT_LADDER = os.getenv(
    "AGENT_LADDER",
    "claude-haiku-4-5-20251001,gemini-3.6-flash,gpt-5.2,claude-sonnet-5",
)

# Hard cap on total attempts (the first try plus escalations) for one request.
AGENT_MAX_ATTEMPTS = int(os.getenv("AGENT_MAX_ATTEMPTS", "3"))

# The spend ceiling for one request, across every attempt and every checker call.
# Before each escalation the loop computes an upper-bound estimate of the next
# attempt; if spend-so-far plus that estimate would cross this, the attempt is not
# made and the best answer already in hand is served. The ceiling is never crossed.
AGENT_COST_CEILING_USD = Decimal(os.getenv("AGENT_COST_CEILING_USD", "0.25"))

# The model the checker uses to judge pass/fail on an answer. Defaults to the same
# small judge model the router uses. Its cost counts toward the ceiling.
AGENT_CHECK_MODEL = os.getenv("AGENT_CHECK_MODEL", JUDGE_MODEL)

# Output-token count assumed for the cost estimate when a request omits max_tokens.
AGENT_DEFAULT_MAX_TOKENS = int(os.getenv("AGENT_DEFAULT_MAX_TOKENS", "1024"))

# --- Evaluation (phase 8): score a sample of routed-down answers, fire-and-forget. ---
# When a request was routed down to a cheaper model (or the agent loop passed on a
# cheap rung), a fraction of those responses are scored with RAGAS in a detached
# background task — never on the request path, never awaited, every failure swallowed.
# The score is written to the eval_scores table and surfaced at /admin/eval/summary.
#
# EVAL_SAMPLE_RATE is the whole on/off switch: 0 disables evaluation entirely (no
# sampling, no scorer built at startup, ragas never imported), and must not break
# boot. 1 scores every qualifying request; anything between is the sampled fraction.
EVAL_SAMPLE_RATE = float(os.getenv("EVAL_SAMPLE_RATE", "0.05"))

# A score at or above this passes; below fails. Both go in the row's `passed` column,
# and the pass rate is what /admin/eval/summary reports.
EVAL_PASS_THRESHOLD = float(os.getenv("EVAL_PASS_THRESHOLD", "0.7"))

# The model RAGAS uses as its judge, wired through langchain-anthropic. Defaults to the
# same small model the router judge uses. Its cost is external (billed by the provider
# on the judge call), not counted against the team budget — evaluation is out-of-band.
EVAL_JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", JUDGE_MODEL)

# Ceiling on how long a single RAGAS metric call may run before it is abandoned. A
# timeout is swallowed like any other failure: the score is simply not recorded.
EVAL_TIMEOUT_SECONDS = float(os.getenv("EVAL_TIMEOUT_SECONDS", "30"))

# --- LangSmith tracing (phase 8): observability for the LangGraph router and loop. ---
# Tracing is entirely env-var driven by LangChain itself. With LANGCHAIN_TRACING_V2
# false (the default) or no LANGCHAIN_API_KEY, LangChain no-ops and slice runs exactly
# as before — no code path here requires LangSmith. When tracing is on, runs are
# grouped under LANGCHAIN_PROJECT (default "slice"); configure_tracing() sets that
# default at startup without ever touching the key.
LANGCHAIN_TRACING_V2 = _bool("LANGCHAIN_TRACING_V2", False)
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY") or None
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT", "slice")

# --- Guardrails (phase 9): NeMo self-check rails around the agent loop only. ---
# Two rails — a self-check on the incoming prompt and a self-check on the assembled
# final answer — wrap the phase-7 agent loop and ONLY the agent loop. Plain proxy
# traffic and the router path never touch them; they run exactly where the loop runs
# (the non-streaming auto-routed-down path). Everything fails open: any error or
# timeout inside the rails engine is logged and the loop proceeds as if the rail
# passed, so a broken rail can never block or crash a request.
#
# GUARDRAILS_ENABLED is the whole kill switch. False means zero rails code runs and
# the loop behaves exactly as phase 7 — and because nemoguardrails is imported lazily
# (only when the engine is built, which only happens when this is on), the server
# starts fine even if nemoguardrails is broken or absent while the switch is off.
GUARDRAILS_ENABLED = _bool("GUARDRAILS_ENABLED", True)

# The model the rails LLM uses for its self-checks, wired through langchain-anthropic
# (ChatAnthropic), the same integration the phase-8 eval judge uses. Defaults to the
# router judge model. Its calls are slice's own infrastructure cost, out-of-band from
# the team budget, exactly like the eval judge.
GUARDRAILS_MODEL = os.getenv("GUARDRAILS_MODEL", JUDGE_MODEL)

# Ceiling on how long a single rail check may run before it is abandoned. A timeout is
# swallowed like any other rails failure: fail open, log, the loop continues.
GUARDRAILS_TIMEOUT_SECONDS = float(os.getenv("GUARDRAILS_TIMEOUT_SECONDS", "5"))

# The NeMo Guardrails config directory (config.yml plus the self-check prompts). Only
# the two built-in self-check rails are enabled there, with custom slice-specific
# prompts and no feature that needs embeddings or downloads a model at runtime.
GUARDRAILS_CONFIG_DIR = os.getenv("GUARDRAILS_CONFIG_DIR", "guardrails")

# --- Dashboard (phase 10): local read endpoints plus a live SSE stream. ---
# Browser origins allowed to call the gateway (CORS), comma-separated. The default is
# the Vite dev server the dashboard runs on; the built dashboard/dist is served by the
# gateway itself and needs no CORS. The /dashboard/* endpoints have no auth yet (phase
# 12), so keep this list to local origins.


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return [item.strip() for item in raw.split(",") if item.strip()]


CORS_ORIGINS = _csv("CORS_ORIGINS", "http://localhost:5173")
