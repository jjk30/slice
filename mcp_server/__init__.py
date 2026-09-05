"""slice MCP server (phase 14).

A standalone stdio MCP server that exposes slice's data as tools inside Claude Code.
It is a *thin adapter* over slice's existing HTTP gateway: it makes short async httpx
calls to the running gateway and shapes the JSON into compact, human-readable text. It
holds no gateway logic, no database access, and no business rules of its own: every
number it reports comes from an endpoint the gateway already serves.

Run it as ``python -m mcp_server`` (stdio). Configuration is two env vars, read by
``mcp_server.config``: ``SLICE_BASE_URL`` (default ``http://localhost:8080``) and
``SLICE_API_KEY`` (the slice key, sent as ``Authorization: Bearer <key>``, the same way
the phase-12 gateway auth reads it). With no key set the server still works against a
gateway running in local/unlocked mode; a call that comes back 401 tells the user to set
the key.

Layout:

- ``config``: env-driven settings.
- ``client``: the httpx wrapper and its typed errors (unreachable / unauthorized / other).
- ``tools``: the tool functions. Pure ``(SliceClient, args) -> str``; this is the layer
  the tests drive with a mocked gateway. No dependency on the ``mcp`` SDK.
- ``server``: wires the tool functions into an ``mcp`` ``FastMCP`` server as stdio tools.
"""
