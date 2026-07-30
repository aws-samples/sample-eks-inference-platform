#!/bin/sh
# db-init.sh — one-time provisioning of the platform_health_agent database.
#
# Connects to platform-db-0 as the existing `platform` superuser
# (credentials from platform-db-credentials), creates the
# `platform_health_agent` database if missing, then runs ddl.sql against it.
#
# Idempotent: re-running is a no-op.
# Run by: db-init Job (ArgoCD PreSync hook on every sync).

set -eu

PG_HOST="${DB_HOST:?DB_HOST not set}"
PG_PORT="${DB_PORT:-5432}"
PG_USER="${PG_SUPERUSER:?PG_SUPERUSER not set}"
PG_PASS="${PG_SUPERPASSWORD:?PG_SUPERPASSWORD not set}"
TARGET_DB="${DB_NAME:-platform_health_agent}"

export PGPASSWORD="$PG_PASS"

echo "[db-init] checking if database '$TARGET_DB' exists on $PG_HOST:$PG_PORT…"
db_exists=$(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$TARGET_DB'" || true)

if [ "$db_exists" != "1" ]; then
    echo "[db-init] creating database '$TARGET_DB'…"
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres \
        -c "CREATE DATABASE $TARGET_DB"
else
    echo "[db-init] database '$TARGET_DB' already exists, skipping create."
fi

echo "[db-init] applying schema (idempotent)…"
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$TARGET_DB" \
    -v ON_ERROR_STOP=1 -f /scripts/ddl.sql

echo "[db-init] verifying tables…"
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$TARGET_DB" -tAc \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"

echo "[db-init] done."
