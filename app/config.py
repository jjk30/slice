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
