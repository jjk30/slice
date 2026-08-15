-- Phase 4: cache hits are logged as request rows too, with cost 0. This column
-- marks those rows so cached traffic is distinguishable from real provider
-- calls. IF NOT EXISTS keeps it idempotent, so it is safe to run on every boot
-- and on a database that predates the column alike.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS cached BOOLEAN NOT NULL DEFAULT false;
