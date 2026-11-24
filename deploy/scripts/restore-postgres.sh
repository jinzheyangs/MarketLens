#!/usr/bin/env bash
# Restore a custom-format MarketLens PostgreSQL backup.
#
# Required:
#   RESTORE_DATABASE_URL=postgresql://user:password@host:5432/dbname
#   RESTORE_DUMP=/path/to/marketlens-....dump
#   RESTORE_CONFIRM=<database name from the URL>
#
# The restore is additive by default. Set RESTORE_CLEAN=true only after taking a
# fresh backup; it drops objects present in the dump before recreating them.
set -euo pipefail

: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL is required}"
: "${RESTORE_DUMP:?RESTORE_DUMP is required}"
: "${RESTORE_CONFIRM:?Set RESTORE_CONFIRM to the target database name}"

if [[ ! -f "$RESTORE_DUMP" ]]; then
  echo "backup not found: $RESTORE_DUMP" >&2
  exit 1
fi

if [[ -f "$RESTORE_DUMP.sha256" ]]; then
  (cd "$(dirname "$RESTORE_DUMP")" && sha256sum --check "$(basename "$RESTORE_DUMP").sha256")
fi

if ! command -v pg_restore >/dev/null 2>&1; then
  echo "pg_restore is required" >&2
  exit 1
fi

db_name="$(printf '%s' "$RESTORE_DATABASE_URL" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')"
if [[ "$RESTORE_CONFIRM" != "$db_name" ]]; then
  echo "RESTORE_CONFIRM does not match target database '$db_name'" >&2
  exit 1
fi

args=(
  --dbname="$RESTORE_DATABASE_URL"
  --no-owner
  --no-privileges
  --exit-on-error
  --verbose
)
if [[ "${RESTORE_CLEAN:-false}" == "true" ]]; then
  args+=(--clean --if-exists)
  echo "WARNING: destructive clean restore into $db_name" >&2
fi

pg_restore "${args[@]}" "$RESTORE_DUMP"
echo "restore_complete $db_name"
