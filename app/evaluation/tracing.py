"""LangSmith tracing wiring (phase 8, part 2).

Tracing is driven entirely by LangChain's own environment variables — there is no
slice code in the router or the agent loop that calls LangSmith. When
``LANGCHAIN_TRACING_V2`` is false (the default) or ``LANGCHAIN_API_KEY`` is unset,
LangChain no-ops and every graph runs exactly as it did before phase 8. Nothing here
is ever required for a request to succeed.

All this module does is set a sensible default project name at startup so traces,
when they are on, group under one name. It never sets the API key and never turns
tracing on for you — those stay explicit, in the environment.
"""

from __future__ import annotations

import json
import logging
import os

from app import config

logger = logging.getLogger("slice.gateway")


def configure_tracing() -> None:
    """Default LANGCHAIN_PROJECT to "slice" when tracing is on. Never raises.

    Called once at startup. If tracing is off, does nothing at all. If it is on but
    no API key is configured, LangChain will simply not export — we log that once so
    the misconfiguration is visible, but the gateway runs unchanged either way.
    """
    if not config.LANGCHAIN_TRACING_V2:
        return

    # Only fill in a default; never overwrite an explicit project from the env.
    os.environ.setdefault("LANGCHAIN_PROJECT", config.LANGCHAIN_PROJECT)

    if config.LANGCHAIN_API_KEY is None:
        logger.warning(
            json.dumps(
                {
                    "event": "tracing_enabled_without_key",
                    "detail": "LANGCHAIN_TRACING_V2 is on but LANGCHAIN_API_KEY is unset; "
                    "LangChain will not export traces.",
                }
            )
        )
        return

    logger.info(
        json.dumps({"event": "tracing_enabled", "project": os.environ["LANGCHAIN_PROJECT"]})
    )
