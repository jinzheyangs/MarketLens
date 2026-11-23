#!/usr/bin/env bash
# Local development convenience: create a separate database for integration tests.
# The test suite refuses to run against a database whose name lacks "test".
set -euo pipefail

test_db="${POSTGRES_TEST_DB:?POSTGRES_TEST_DB must be set}"

psql \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  -v ON_ERROR_STOP=1 \
  -v test_db="${test_db}" <<'SQL'
SELECT format('CREATE DATABASE %I', :'test_db')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'test_db')
\gexec
SQL
