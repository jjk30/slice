-- Phase 12: auth. Accounts (one per GitHub user), slice keys (the bearer credential
-- the gateway checks), and an account_id on every table the gateway writes so each
-- account only ever reads its own rows. Everything here is idempotent (CREATE ... IF NOT
-- EXISTS / ADD COLUMN IF NOT EXISTS), safe to run on every boot and on a database that
-- predates it alike, exactly like the earlier migrations.
--
-- accounts: keyed by the GitHub numeric id (stable; the login can be renamed on GitHub
-- and is refreshed on every login). email is whatever /user returned, often NULL.
CREATE TABLE IF NOT EXISTS accounts (
    id           BIGSERIAL   PRIMARY KEY,
    github_id    BIGINT      NOT NULL UNIQUE,
    github_login TEXT,
    email        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- slice_keys: only the SHA-256 of the key is stored (never the key itself, which is shown
-- once at mint time). key_prefix is a short display string ("slk_live_ab12...") so a key
-- can be recognised in a list without ever being recoverable from the row. revoked_at
-- set means the key is dead: verification treats it exactly like a missing key.
CREATE TABLE IF NOT EXISTS slice_keys (
    id           BIGSERIAL   PRIMARY KEY,
    account_id   BIGINT      NOT NULL REFERENCES accounts (id),
    key_hash     TEXT        NOT NULL UNIQUE,
    key_prefix   TEXT        NOT NULL,
    name         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS slice_keys_account_idx ON slice_keys (account_id);

-- The account is the tenant. Every table the gateway writes learns which account the row
-- belongs to; every /admin and /dashboard read filters on it. Nullable on purpose: rows
-- that predate auth stay NULL (they belong to nobody and are shown to nobody), and the
-- fire-and-forget writers keep working exactly as before with a NULL here.
ALTER TABLE requests         ADD COLUMN IF NOT EXISTS account_id BIGINT REFERENCES accounts (id);
ALTER TABLE switch_rules     ADD COLUMN IF NOT EXISTS account_id BIGINT REFERENCES accounts (id);
ALTER TABLE eval_scores      ADD COLUMN IF NOT EXISTS account_id BIGINT REFERENCES accounts (id);
ALTER TABLE guardrail_events ADD COLUMN IF NOT EXISTS account_id BIGINT REFERENCES accounts (id);
ALTER TABLE alerts           ADD COLUMN IF NOT EXISTS account_id BIGINT REFERENCES accounts (id);

-- The dashboard filters "this month for this account", and the recent list walks one
-- account's rows newest-first, so (account_id, created_at) is the index that keeps both
-- cheap. The other tables are small (bounded by sampling / cooldowns) but get the same.
CREATE INDEX IF NOT EXISTS requests_account_created_idx         ON requests (account_id, created_at);
CREATE INDEX IF NOT EXISTS switch_rules_account_idx             ON switch_rules (account_id);
CREATE INDEX IF NOT EXISTS eval_scores_account_created_idx      ON eval_scores (account_id, created_at);
CREATE INDEX IF NOT EXISTS guardrail_events_account_created_idx ON guardrail_events (account_id, created_at);
CREATE INDEX IF NOT EXISTS alerts_account_ts_idx                ON alerts (account_id, ts);
