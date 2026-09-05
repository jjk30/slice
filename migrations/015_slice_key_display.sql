-- Phase 22a: a masked display form for a slice key, so the dashboard can show
-- `slk_live_••••••••a1b2` without ever storing the key itself. `prefix` is the fixed
-- `slk_live_` marker and `last4` is the key's final four characters, both captured at
-- mint time next to the SHA-256 hash (migrations/010_auth.sql). The plain key is never
-- stored; last4 is four public characters, far too few to reconstruct it. Existing rows
-- predate the columns: prefix is backfilled to the known marker, and last4 stays NULL
-- (the plain key is gone, so its last four are unknowable), the dashboard renders that
-- as the prefix and dots. Adding with IF NOT EXISTS keeps this idempotent, like 013/014,
-- safe to run on every boot and on a database that predates the columns alike.
ALTER TABLE slice_keys ADD COLUMN IF NOT EXISTS prefix TEXT;
ALTER TABLE slice_keys ADD COLUMN IF NOT EXISTS last4  TEXT;
UPDATE slice_keys SET prefix = 'slk_live_' WHERE prefix IS NULL;
