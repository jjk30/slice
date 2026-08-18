-- Phase 9: guardrails. Two NeMo self-check rails wrap the agent loop — one on the
-- incoming prompt, one on the assembled final answer. Every time a rail blocks a
-- request or errors out (fail-open), it lands here as its own row, so the local
-- /admin/guardrails/summary endpoint can report what the rails have been doing.
-- CREATE TABLE IF NOT EXISTS keeps this idempotent, safe to run on every boot and on
-- a database that predates the table alike, exactly like the earlier migrations.
--
-- request_id is the served request this event belongs to. It is nullable and carries
-- no foreign key on purpose, mirroring eval_scores (migration 006): the request row is
-- written fire-and-forget and its id is never read back, so the writer usually has
-- nothing to link to. It exists so a later phase that does capture the id can populate
-- it without another migration.
--
-- rail is 'input' or 'output'; action is 'blocked' or 'error'. reason is a short free
-- text note (the rail name for a block, or the error string for a fail-open error).
CREATE TABLE IF NOT EXISTS guardrail_events (
    id          BIGSERIAL   PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id  BIGINT,
    team        TEXT,
    rail        TEXT        NOT NULL,
    action      TEXT        NOT NULL,
    reason      TEXT
);

-- The summary endpoint counts per rail and per action, so an index on those keeps the
-- aggregation cheap as the table grows.
CREATE INDEX IF NOT EXISTS guardrail_events_rail_idx ON guardrail_events (rail);
CREATE INDEX IF NOT EXISTS guardrail_events_action_idx ON guardrail_events (action);
