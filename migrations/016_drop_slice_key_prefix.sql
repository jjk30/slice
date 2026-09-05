-- Phase 22c: drop the duplicate `prefix` column on slice_keys. Migration 015 added
-- `prefix` to hold the fixed `slk_live_` marker, but `key_prefix` (migrations/010_auth.sql)
-- already stores a `slk_live_…` display string, and the marker is a constant the code knows
-- on its own, so the column was pure duplication. `last4` stays; the masked card renders
-- the constant marker plus the stored last four. DROP ... IF EXISTS keeps this idempotent,
-- like every migration before it, safe to run on every boot and on a database that predates
-- the column alike.
ALTER TABLE slice_keys DROP COLUMN IF EXISTS prefix;
