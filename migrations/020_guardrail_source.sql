-- Phase 28: where a guardrail event came from. The gateway's agent-loop rails and the
-- email assistant's rails both land in guardrail_events now, told apart by source:
-- 'gateway' for the agent loop (the only writer before this migration, hence the
-- default, which also stamps every existing row) and 'email' for the email assistant.
-- The dashboard's "guardrail blocks this month" tile splits its count by this column.
-- ADD COLUMN IF NOT EXISTS keeps it idempotent, safe to run on every boot.
ALTER TABLE guardrail_events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'gateway';
