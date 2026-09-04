-- Phase 24b: scan expectations. A user marks one finding as expected ("this bucket is
-- public on purpose, stop telling me") and the scanner leaves that (check, resource)
-- out of the new-high alert email. Findings are still recorded in scan_findings on every
-- run; expectations only change alerting, never the record. account_id is the same
-- storage scope scan_findings uses: NULL means slice's own account (the operator), else
-- the account id. The uniqueness index folds NULL into 0 the way aws_costs does, so one
-- (scope, check, resource) has exactly one row, own account included. Removing an
-- expectation sets removed_at rather than deleting the row: the next scan uses that
-- time to treat the finding as new again (so undoing brings the email back once), and
-- marking it expected again just clears removed_at. CREATE TABLE IF NOT EXISTS keeps
-- this idempotent, safe to run on every boot like every other migration.
CREATE TABLE IF NOT EXISTS scan_expectations (
    id          BIGSERIAL   PRIMARY KEY,
    account_id  BIGINT,
    "check"     TEXT        NOT NULL,
    resource_id TEXT        NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at  TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS scan_expectations_scope_idx
    ON scan_expectations (COALESCE(account_id, 0), "check", resource_id);
