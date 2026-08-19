"""Wire the slice tools into an ``mcp`` FastMCP stdio server.

Thin on purpose: this module only maps the pure tool coroutines in ``mcp_server.tools``
onto the official SDK's ``@mcp.tool()`` decorators, giving each the user-facing signature
Claude Code sees (no ``SliceClient`` argument — that is supplied internally from one
shared, long-lived client). All behavior, formatting, and error handling live in
``tools``; nothing gateway-specific lives here.

The shared ``SliceClient`` is opened when the server starts and closed on shutdown via the
server's lifespan, so the process holds exactly one httpx client.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from mcp_server import tools
from mcp_server.client import SliceClient
from mcp_server.config import Settings

INSTRUCTIONS = (
    "Tools for inspecting a running slice LLM gateway: current-month spend vs budget, "
    "switch rules, recent requests, and the RAGAS eval pass rate. The two write tools "
    "(add_rule, delete_rule) use a confirm handshake: call once to preview, then again "
    "with confirm=true to apply."
)


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    # One client for the whole process. httpx builds its connection pool lazily, so
    # constructing it here (before the event loop) is fine; the lifespan closes it.
    client = SliceClient.build(settings)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            await client.aclose()

    mcp = FastMCP(name="slice", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool(description="Current-month slice spend vs budget, with the warn ratio and whether the cap is hit.")
    async def get_spend() -> str:
        return await tools.get_spend(client)

    @mcp.tool(description="List the current switch (model-routing) rules for the slice account.")
    async def list_rules() -> str:
        return await tools.list_rules(client)

    @mcp.tool(
        description=(
            "The last N slice requests (model, status, cost, routed-from, cached, time). "
            "N defaults to 10 and is capped at 50."
        )
    )
    async def get_recent_requests(limit: int = tools.RECENT_DEFAULT) -> str:
        return await tools.get_recent_requests(client, limit)

    @mcp.tool(description="The slice RAGAS eval pass-rate summary (overall and per model).")
    async def get_eval_summary() -> str:
        return await tools.get_eval_summary(client)

    @mcp.tool(
        description=(
            "Add a slice switch rule routing from_model to to_model for a team. "
            "Previews the change unless confirm=true is passed, which applies it."
        )
    )
    async def add_rule(
        team: str, from_model: str, to_model: str, confirm: bool = False
    ) -> str:
        return await tools.add_rule(client, team, from_model, to_model, confirm)

    @mcp.tool(
        description=(
            "Delete a slice switch rule by its id. Previews the deletion unless "
            "confirm=true is passed, which applies it."
        )
    )
    async def delete_rule(rule_id: int, confirm: bool = False) -> str:
        return await tools.delete_rule(client, rule_id, confirm)

    return mcp
