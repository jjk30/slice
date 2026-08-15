import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
PORT = int(os.getenv("PORT", "8080"))

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
