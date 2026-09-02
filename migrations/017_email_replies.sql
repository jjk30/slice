-- Phase 23b: the reply-by-email assistant. One row per inbound mail Resend delivers to
-- POST /email/inbound, keyed by Resend's own email_id so a webhook retry (Resend resends
-- on a non-2xx or a timeout) is a no-op: the pipeline claims the row with an INSERT ...
-- ON CONFLICT DO NOTHING before doing anything else, and a duplicate stops there. verdict
-- is one of no_account, ignored, blocked_input, blocked_output, answered, error; the row
-- is claimed as 'error' and updated to the real verdict at the end, so a pipeline that
-- dies mid-way leaves the truth behind. account_id is NULL until the sender is matched
-- (and stays NULL for a stranger; nothing about them is stored beyond the address). The
-- subject is kept; the body and the answer are never stored or logged in full. CREATE
-- TABLE IF NOT EXISTS keeps this idempotent, safe to run on every boot like every other.
CREATE TABLE IF NOT EXISTS email_replies (
    id           BIGSERIAL   PRIMARY KEY,
    account_id   BIGINT      REFERENCES accounts (id),
    email_id     TEXT        NOT NULL UNIQUE,
    from_address TEXT        NOT NULL,
    subject      TEXT,
    verdict      TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_replies_account_created_idx ON email_replies (account_id, created_at);
