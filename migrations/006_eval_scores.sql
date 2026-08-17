-- Phase 8: evaluation. A sampled slice of routed-down answers is scored offline
-- (RAGAS), fire-and-forget, off the request path. Each score lands here as its own
-- row: one row per (request, metric), so a single sampled request that is scored on
-- both answer relevancy and context relevance writes two rows. CREATE TABLE
-- IF NOT EXISTS keeps this idempotent, safe to run on every boot and on a database
-- that predates the table alike.
--
-- request_id is the served request this score belongs to. It is nullable and carries
-- no foreign key on purpose: the request row is written fire-and-forget and its id is
-- never read back, so the scorer usually has nothing to link to. It exists so a later
-- phase that does capture the id can populate it without another migration.
CREATE TABLE IF NOT EXISTS eval_scores (
    id          BIGSERIAL        PRIMARY KEY,
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    request_id  BIGINT,
    model       TEXT,
    routed_from TEXT,
    metric      TEXT             NOT NULL,
    score       DOUBLE PRECISION NOT NULL,
    passed      BOOLEAN          NOT NULL,
    judge_model TEXT
);

-- The summary endpoint groups by model and by the (routed_from, model) pair, so an
-- index on those keeps the aggregation cheap as the table grows.
CREATE INDEX IF NOT EXISTS eval_scores_model_idx ON eval_scores (model);
CREATE INDEX IF NOT EXISTS eval_scores_route_idx ON eval_scores (routed_from, model);
