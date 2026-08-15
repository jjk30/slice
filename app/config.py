import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
PORT = int(os.getenv("PORT", "8080"))

# Unset means request logging is disabled; the proxy still serves traffic.
DATABASE_URL = os.getenv("DATABASE_URL") or None
