#!/usr/bin/env bash
# Run the whole stack for local development:
#   - the gateway (proxy + stats API) on :8080
#   - the dashboard (Vite) on :5173
#
# Assumes Postgres + Redis are already up (cd gateway && docker compose up -d).
# Ctrl-C stops both. For more control, run each app in its own terminal instead
# (see the README).
set -euo pipefail
cd "$(dirname "$0")"

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "→ starting gateway on http://localhost:8080"
(cd gateway && npm run dev) &

echo "→ starting dashboard on http://localhost:5173"
(cd dashboard && npm run dev) &

wait
