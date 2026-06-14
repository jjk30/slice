-- slice Phase 2 schema.
-- Runs automatically the FIRST time the Postgres container starts, via the
-- docker-entrypoint-initdb.d mount in docker-compose.yml. Zero manual setup.
--
-- One row per proxied request. Columns mirror the RequestLog shape in
-- src/logger.ts exactly, plus an auto-increment id and a created_at default.

CREATE TABLE IF NOT EXISTS requests (
  id            BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  method        TEXT        NOT NULL,
  path          TEXT        NOT NULL,
  model         TEXT,                       -- nullable: absent on non-JSON bodies
  status        INTEGER     NOT NULL,
  latency_ms    INTEGER     NOT NULL,
  input_tokens  INTEGER,                    -- nullable: only when upstream reports usage
  output_tokens INTEGER,                    -- nullable: only when upstream reports usage
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Handy for the "most recent requests" query in the acceptance steps.
CREATE INDEX IF NOT EXISTS requests_created_at_idx ON requests (created_at DESC);
