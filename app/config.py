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
