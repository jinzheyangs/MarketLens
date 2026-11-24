#!/usr/bin/env bash
# Verify checksum and archive readability without touching a database.
set -euo pipefail

: "${1:?usage: verify-backup.sh <dump>}"

dump="$1"
if [[ -f "$dump.sha256" ]]; then
  (cd "$(dirname "$dump")" && sha256sum --check "$(basename "$dump").sha256")
fi
if ! command -v pg_restore >/dev/null 2>&1; then
  echo "pg_restore is required" >&2
  exit 1
fi
pg_restore --list "$dump" >/dev/null
echo "backup_verified $dump"
