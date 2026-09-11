-- Phase 32: human in the loop approvals for MCP writes. An agent never writes a rule
-- itself: it proposes a change (POST /actions/propose), the row lands here, and the
-- account owner gets an email with an Approve link and a Reject link. Only the approval
-- applies the write. kind is add_rule or delete_rule; payload is the validated body the
-- write would have carried ({team, from_model, to_model} or {rule_id}); token_hash is
-- the SHA-256 of the one-use link token (the token itself is never stored); status is
-- pending, approved, rejected or expired; decided_via records how the decision came in
-- (email for now); applied_rule_id is the rule the approval created or deleted, for the
-- status read. Rows expire ten minutes after they are proposed and are never reused.
-- CREATE TABLE IF NOT EXISTS keeps this idempotent, safe to run on every boot.
CREATE TABLE IF NOT EXISTS pending_actions (
    id              SERIAL      PRIMARY KEY,
    account_id      BIGINT      REFERENCES accounts (id),
    kind            TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    token_hash      TEXT        NOT NULL UNIQUE,
    status          TEXT        NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ,
    decided_via     TEXT,
    applied_rule_id INTEGER
);

CREATE INDEX IF NOT EXISTS pending_actions_account_status_idx ON pending_actions (account_id, status);
