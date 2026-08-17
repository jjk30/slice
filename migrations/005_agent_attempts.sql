-- Phase 7: the agent loop can make more than one provider call for a single
-- request (try cheap, check, escalate). This column records how many attempts
-- were made, so a served row shows whether it took one try or an escalation. It
-- defaults to 1 -- the value every non-loop request logs -- so existing rows and
-- non-auto paths are unaffected. IF NOT EXISTS keeps it idempotent, safe to run
-- on every boot and on a database that predates the column alike.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 1;
