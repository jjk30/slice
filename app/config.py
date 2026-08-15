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
