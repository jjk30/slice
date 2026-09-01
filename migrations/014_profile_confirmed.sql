-- Phase 21: mark when an account has finished the dashboard's first-time setup
-- screen, so it is shown once and never again. NULL until the first profile save;
-- update_account_profile (app/db.py) stamps it now() on every successful save, and
-- GET /account/profile reports profile_confirmed = (this column is not NULL). Adding
-- it with IF NOT EXISTS keeps the migration self-describing and idempotent, like 013 -
-- safe to run on every boot and on a database that predates the column alike.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS profile_confirmed_at TIMESTAMPTZ;
