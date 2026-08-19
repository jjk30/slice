"""Entry point: ``python -m mcp_server`` runs the slice MCP server over stdio.

Reads ``SLICE_BASE_URL`` / ``SLICE_API_KEY`` from the environment (see
``mcp_server.config``), builds the FastMCP server, and hands control to the ``mcp`` SDK's
stdio transport. Claude Code (or any MCP client) speaks to it over stdin/stdout.
"""

from __future__ import annotations

from mcp_server.server import build_server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
