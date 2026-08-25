-- Phase 20: the account profile the desktop app (and, later, the dashboard) lets a
-- user set — an email and a WhatsApp number slice can reach them on. Both are
-- nullable and edited through PUT /account/profile. `email` already exists on
-- accounts (it is the GitHub email captured at login, migrations/010_auth.sql), so
-- this restates it with IF NOT EXISTS to keep the migration self-describing and
-- idempotent, and adds the new whatsapp_number column. Safe to run on every boot and
-- on a database that predates the columns alike.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email           TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS whatsapp_number TEXT;
