-- Phase 18a: AWS security scanner. Two tables, both written fire-and-forget from a
-- detached background task (never the request path), exactly like request logging and
-- alerts: a down database drops the write, it never blocks or raises. CREATE TABLE IF
-- NOT EXISTS keeps this idempotent, safe to run on every boot.

-- scan_findings: one row per finding per scan run. run_id ties a scan's findings
-- together (a uuid hex minted per run). check is which check raised it ('s3_public',
-- 'sg_open', 'unencrypted', 'iam_risk'); it is quoted throughout because CHECK is a SQL
-- keyword. resource_id points at the bucket / group / volume / key / user. severity is
-- 'high', 'med' or 'low': the alert path watches 'high'. summary is one plain sentence;
-- detail is the specifics (open port, key age, grantee) as JSONB.
CREATE TABLE IF NOT EXISTS scan_findings (
    id          BIGSERIAL   PRIMARY KEY,
    run_id      TEXT        NOT NULL,
    "check"     TEXT        NOT NULL,
    resource_id TEXT        NOT NULL,
    severity    TEXT        NOT NULL,
    summary     TEXT        NOT NULL,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The findings endpoint reads one run's rows (or the newest run's); the alert path reads
-- the newest run's highs and the run before it. Both filter by run_id and order by time.
CREATE INDEX IF NOT EXISTS scan_findings_run_idx ON scan_findings (run_id);
CREATE INDEX IF NOT EXISTS scan_findings_created_idx ON scan_findings (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS scan_findings_severity_idx ON scan_findings (severity);

-- aws_costs: one row per completed calendar day, upserted on each Cost Explorer pull
-- (date is the primary key, so re-fetching a day just refreshes its amount and
-- fetched_at). yesterday's spend is the newest row; month-to-date is the sum of the
-- current month's rows. Cost Explorer bills $0.01 per call, so the fetch is latched to
-- once per day in Redis: this table is just where the numbers land.
CREATE TABLE IF NOT EXISTS aws_costs (
    date       DATE          PRIMARY KEY,
    amount_usd NUMERIC(14, 2) NOT NULL,
    fetched_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);
