-- Phase 10: the dashboard. Its read endpoints filter every table by "this month"
-- (created_at >= the first instant of the current UTC month) and the recent list
-- walks requests newest-first, so created_at gets an index on each table the
-- dashboard reads. No schema change to any row shape; nothing the request path
-- writes is affected. CREATE INDEX IF NOT EXISTS keeps this idempotent, safe to run
-- on every boot and on a database that predates the indexes alike, exactly like the
-- earlier migrations.
CREATE INDEX IF NOT EXISTS requests_created_at_idx ON requests (created_at);
CREATE INDEX IF NOT EXISTS eval_scores_created_at_idx ON eval_scores (created_at);
CREATE INDEX IF NOT EXISTS guardrail_events_created_at_idx ON guardrail_events (created_at);
