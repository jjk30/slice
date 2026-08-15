-- Phase 5: the router. Two schema changes, both idempotent so they are safe to
-- run on every boot and on a database that predates them alike.

-- Per-team switch rules. An exact (team, from_model) match swaps the request to
-- to_model, ahead of the auto judge and regardless of whether auto-routing is on.
CREATE TABLE IF NOT EXISTS switch_rules (
    id         BIGSERIAL   PRIMARY KEY,
    team       TEXT        NOT NULL,
    from_model TEXT        NOT NULL,
    to_model   TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- When a request is routed, the client's original model is recorded here and the
-- model it was actually served with goes in the existing `model` column. NULL
-- means the request was served with the model the client asked for.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS routed_from TEXT;
