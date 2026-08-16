-- Phase 6: the RAG index is built offline from past prompts, so the prompt text
-- has to be logged alongside every request. This column holds the concatenated
-- user-role text of the request, capped at 4000 chars by the writer, NULL when no
-- prompt could be extracted. IF NOT EXISTS keeps it idempotent, so it is safe to
-- run on every boot and on a database that predates the column alike.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS prompt_text TEXT;
