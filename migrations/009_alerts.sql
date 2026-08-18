-- Phase 11: alerts. When a team crosses its budget warn line (kind 'warn') or hits its
-- cap (kind 'block'), the alerts engine tries every configured channel and writes one
-- row per attempt here, fire-and-forget, exactly like request logging: a down database
-- never blocks or raises. The local /admin/alerts/summary endpoint reads it back.
-- CREATE TABLE IF NOT EXISTS keeps this idempotent, safe to run on every boot and on a
-- database that predates the table alike, exactly like the earlier migrations.
--
-- kind is 'warn' or 'block'. channel names the delivery channel ('email' for now; Slack
-- and WhatsApp land later as their own values). status is 'sent' (the channel accepted
-- it), 'failed' (non-2xx or an exception, swallowed), or 'skipped_cooldown' (an alert of
-- this kind for this team went out inside ALERT_COOLDOWN_SECONDS, so nothing was sent).
-- detail is a short JSON note: spend so far, the cap, the month, and the error on a
-- failure. ts is when the engine made the attempt.
CREATE TABLE IF NOT EXISTS alerts (
    id       BIGSERIAL   PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    team     TEXT,
    kind     TEXT        NOT NULL,
    channel  TEXT        NOT NULL,
    status   TEXT        NOT NULL,
    detail   TEXT
);

-- The summary endpoint counts per kind and per status and lists the newest rows, so
-- those get indexes to keep it cheap as the table grows.
CREATE INDEX IF NOT EXISTS alerts_kind_idx ON alerts (kind);
CREATE INDEX IF NOT EXISTS alerts_status_idx ON alerts (status);
CREATE INDEX IF NOT EXISTS alerts_ts_idx ON alerts (ts);
