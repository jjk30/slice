"""Wire the slice tools into an ``mcp`` FastMCP stdio server.

Thin on purpose: this module only maps the pure tool coroutines in ``mcp_server.tools``
onto the official SDK's ``@mcp.tool()`` decorators, giving each the user-facing signature
Claude Code sees (no ``SliceClient`` argument, that is supplied internally from one
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
    "slice, the AI cost gateway. You're connected to a running slice gateway, so when "
    "the user first engages, greet them warmly, mention they're connected to slice, and "
    "offer to show how much of their monthly API budget is left (call get_spend).\n\n"
    "What you can do here: check spend vs budget (get_spend), list model-routing rules "
    "(list_rules), propose adding or deleting a rule (add_rule, delete_rule), check where "
    "a proposal stands (get_action_status), show recent requests (get_recent_requests), "
    "and show the eval pass rate (get_eval_summary).\n\n"
    "Write safety: add_rule and delete_rule never change anything themselves. They send a "
    "proposal to the gateway, which emails the account owner an approve link and a reject "
    "link; only the owner's approval applies the change, and it expires after ten minutes. "
    "After proposing, tell the user to check their email, and use get_action_status with "
    "the action id to see whether it was approved."
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
            "Propose a slice switch rule routing from_model to to_model for a team. Nothing "
            "changes until the account owner approves it from the email slice sends them; "
            "returns the action id to check with get_action_status."
        )
    )
    async def add_rule(team: str, from_model: str, to_model: str) -> str:
        return await tools.add_rule(client, team, from_model, to_model)

    @mcp.tool(
        description=(
            "Propose deleting a slice switch rule by its id. Nothing changes until the "
            "account owner approves it from the email slice sends them; returns the action "
            "id to check with get_action_status."
        )
    )
    async def delete_rule(rule_id: int) -> str:
        return await tools.delete_rule(client, rule_id)

    @mcp.tool(
        description=(
            "The status of a proposed action by its id: pending, approved (with the rule id "
            "it applied), rejected, or expired."
        )
    )
    async def get_action_status(action_id: int) -> str:
        return await tools.get_action_status(client, action_id)

    return mcp
