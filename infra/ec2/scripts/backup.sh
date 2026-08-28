#!/usr/bin/env bash
# Nightly Postgres backup for the slice cheap EC2 box.
#
# Postgres runs as the docker compose service "postgres" (image postgres:16) in
# /opt/slice, with its credentials in /opt/slice/.env (DB_USERNAME, DB_NAME,
# POSTGRES_PASSWORD). This script dumps that database in custom compressed format,
# verifies the dump with pg_restore --list, uploads it to S3, then removes the
# local file. It writes exactly one line per run to /var/log/slice-backup.log and
# fails loud: any error is logged and the exit code is non-zero.
#
# The backup bucket is passed in as SLICE_BACKUP_BUCKET (set in the cron file), so
# no bucket name is baked into this committed script.
set -euo pipefail

LOG=/var/log/slice-backup.log
COMPOSE_DIR=/opt/slice
DATE="$(date -u +%Y-%m-%d)"
DUMP="/tmp/slice-$DATE.dump"
KEY="slice-$DATE.dump"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1" >>"$LOG"
}

fail() {
  log "FAILURE: $1"
  rm -f "$DUMP"
  exit 1
}

# Backstop: any unguarded error still logs a failure line instead of exiting silent.
trap 'fail "unexpected error at line $LINENO"' ERR

: "${SLICE_BACKUP_BUCKET:?SLICE_BACKUP_BUCKET is not set}"

cd "$COMPOSE_DIR" || fail "cannot cd to $COMPOSE_DIR"

# Load DB_USERNAME, DB_NAME, and POSTGRES_PASSWORD from the box's compose env file.
set -a
# shellcheck disable=SC1091
. "$COMPOSE_DIR/.env" || fail "cannot read $COMPOSE_DIR/.env"
set +a

# Dump inside the running container. Custom format (-Fc) is compressed by default.
docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  pg_dump -U "$DB_USERNAME" -d "$DB_NAME" -Fc >"$DUMP" || fail "pg_dump failed"

# Sanity check the dump before it is uploaded. pg_restore --list reads the archive
# and errors on a truncated or corrupt file. Run it in the container so the host
# needs no Postgres client tools.
docker compose exec -T postgres pg_restore --list <"$DUMP" >/dev/null \
  || fail "pg_restore --list failed, dump is not valid"

# Upload only after the check passed.
aws s3 cp "$DUMP" "s3://$SLICE_BACKUP_BUCKET/$KEY" || fail "aws s3 cp failed"

rm -f "$DUMP"
log "SUCCESS: uploaded s3://$SLICE_BACKUP_BUCKET/$KEY"
