#!/usr/bin/env bash
# Logical PostgreSQL backup with checksum, optional S3 upload, and retention.
#
# Required:
#   BACKUP_DATABASE_URL=postgresql://user:password@host:5432/dbname
# Optional:
#   BACKUP_DIR=/var/backups/marketscope
#   BACKUP_RETENTION_DAYS=14
#   BACKUP_S3_URI=s3://bucket/prefix
#   BACKUP_KMS_KEY_ID=arn:aws:kms:...
set -euo pipefail

: "${BACKUP_DATABASE_URL:?BACKUP_DATABASE_URL is required}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/marketscope}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

umask 077
mkdir -p "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$BACKUP_DIR/marketscope-$timestamp.dump"
checksum="$dump.sha256"

if ! command -v pg_dump >/dev/null 2>&1; then
  echo "pg_dump is required" >&2
  exit 1
fi

pg_dump \
  --format=custom \
  --compress=9 \
  --no-owner \
  --no-privileges \
  --file="$dump" \
  "$BACKUP_DATABASE_URL"

sha256sum "$dump" > "$checksum"

if [[ -n "${BACKUP_S3_URI:-}" ]]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "aws CLI is required when BACKUP_S3_URI is set" >&2
    exit 1
  fi
  sse_args=(--sse AES256)
  if [[ -n "${BACKUP_KMS_KEY_ID:-}" ]]; then
    sse_args=(--sse aws:kms --sse-kms-key-id "$BACKUP_KMS_KEY_ID")
  fi
  aws s3 cp "$dump" "$BACKUP_S3_URI/$(basename "$dump")" \
    --only-show-errors "${sse_args[@]}"
  aws s3 cp "$checksum" "$BACKUP_S3_URI/$(basename "$checksum")" \
    --only-show-errors "${sse_args[@]}"
fi

find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'marketscope-*.dump' -o -name 'marketscope-*.dump.sha256' \) \
  -mtime "+$RETENTION_DAYS" -delete

echo "backup_complete $dump"
