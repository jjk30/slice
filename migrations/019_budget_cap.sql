-- Phase 25: a per-account monthly budget cap. NULL means "use the default from config"
-- (BUDGET_MONTHLY_USD); a value is the cap the user set from the dashboard's Settings
-- screen (PUT /account/budget, cookie session only). Never backfilled: an account that
-- has not set a cap keeps following the default, even if the operator changes it later.
-- Two decimals because the endpoint accepts dollars and cents and nothing finer.
-- ADD COLUMN IF NOT EXISTS keeps this idempotent, safe to run on every boot.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS budget_cap_usd NUMERIC(12,2);
