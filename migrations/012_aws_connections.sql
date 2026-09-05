-- Phase 18b: users connect their own AWS account for slice to scan via a cross-account
-- read-only role. One connection per account, and every finding / cost row now carries the
-- account it belongs to. Idempotent (IF NOT EXISTS / IF EXISTS) like every migration, safe
-- to run on every boot.

-- Per-account connection. external_id is the confused-deputy guard: a random secret slice
-- generates once per account and requires on every sts:AssumeRole (it survives a
-- disconnect, so reconnecting reuses it, see below). role_arn is the role the user
-- created; status is 'pending' (external id issued, role not yet verified), 'connected'
-- (a live assume-role + read succeeded), or 'error' (last verification/scan failed).
-- last_error is the human-readable reason for the latest error. A DELETE /scanner/connect
-- does NOT drop this row: it resets role_arn/status to pending and keeps external_id
-- reserved for the account.
CREATE TABLE IF NOT EXISTS aws_connections (
    id           BIGSERIAL   PRIMARY KEY,
    account_id   BIGINT      NOT NULL UNIQUE REFERENCES accounts (id),
    role_arn     TEXT,
    external_id  TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',
    last_error   TEXT,
    connected_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Findings and costs become per-account. NULL account_id means slice's own account (the
-- operator's part-A data), so existing rows stay valid and keep being read by the operator.
ALTER TABLE scan_findings ADD COLUMN IF NOT EXISTS account_id BIGINT;
ALTER TABLE aws_costs     ADD COLUMN IF NOT EXISTS account_id BIGINT;

CREATE INDEX IF NOT EXISTS scan_findings_account_idx ON scan_findings (account_id);

-- aws_costs was keyed on date alone in part A; now it is per (account, date). Drop the old
-- date-only primary key and enforce uniqueness per account instead. COALESCE(account_id, 0)
-- folds the NULL (own-account) rows into a single logical key value 0 (no real account has
-- id 0), so one day has exactly one row per account, own account included. The writer does
-- a per-row delete+insert scoped by account, so it does not depend on ON CONFLICT here.
ALTER TABLE aws_costs DROP CONSTRAINT IF EXISTS aws_costs_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS aws_costs_account_date_idx
    ON aws_costs (COALESCE(account_id, 0), date);
