# slice-gateway

Command-line client for slice, a self-hosted LLM gateway that routes, caches, and caps your AI spend.

This package installs the `slice` command. It logs you in through GitHub, then prints the environment lines that point your existing tools at a slice gateway. It has no server dependencies. It needs only typer and httpx, plus the standard library.

## Quickstart

1. Install the CLI:

       pip install slice-gateway

2. Log in through GitHub:

       slice login

3. Point a tool at slice. For Claude Code, set three variables:

       export ANTHROPIC_BASE_URL=https://api.sliceapp.dev
       export ANTHROPIC_API_KEY=sk-ant-api...     # your own Anthropic key
       export ANTHROPIC_AUTH_TOKEN=slk_live_...   # your slice key, from slice login

`ANTHROPIC_AUTH_TOKEN` goes out as `Authorization: Bearer`, which is where slice reads its key. `ANTHROPIC_API_KEY` stays your own Anthropic key in `x-api-key`, and slice forwards it upstream.

Run `slice use anthropic`, `slice use openai`, `slice use curl`, or `slice use claude-code` to print the exact lines for your tool.

## The gateway

This is only the client. The slice gateway itself, the server that does the routing, caching, budgets, and dashboards, lives on GitHub at https://github.com/jjk30/slice.

Homepage: https://sliceapp.dev
