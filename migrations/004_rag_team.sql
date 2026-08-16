-- Phase 6 follow-up: the RAG index is per-team, so each request row records which
-- team it belongs to. The build script groups rows by this column into one index
-- per team. Nullable and NULL for rows that predate the column; the writer stores
-- the same team value main.py already computes ("default" when the header is
-- absent). IF NOT EXISTS keeps it idempotent, safe to run on every boot.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS team TEXT;
