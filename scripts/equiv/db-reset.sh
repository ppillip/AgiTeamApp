#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$SCRIPT_DIR/env.sh"

echo "[equiv] starting PostgreSQL"
compose up -d equiv-db

echo "[equiv] waiting for PostgreSQL health"
for _ in $(seq 1 60); do
  if compose exec -T equiv-db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
compose exec -T equiv-db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null

dbs=("$AGITEAMAPP_EQUIV_TEMPLATE_DB" "$AGITEAMAPP_EQUIV_PY_DB" "$AGITEAMAPP_EQUIV_RS_DB")
for db in "${dbs[@]}"; do
  echo "[equiv] recreate database $db"
  compose exec -T equiv-db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db';" \
    -c "DROP DATABASE IF EXISTS \"$db\";" \
    -c "CREATE DATABASE \"$db\";"
done

for db in "$AGITEAMAPP_EQUIV_PY_DB" "$AGITEAMAPP_EQUIV_RS_DB"; do
  echo "[equiv] apply Python migrations to $db"
  for migration in "$APP_DIR"/backend/migrations/*.sql; do
    compose exec -T equiv-db psql -U "$POSTGRES_USER" -d "$db" -v ON_ERROR_STOP=1 -f "/migrations/$(basename "$migration")" >/dev/null
  done
  echo "[equiv] apply seed to $db"
  compose exec -T equiv-db psql -U "$POSTGRES_USER" -d "$db" -v ON_ERROR_STOP=1 -f /equiv/seed.sql >/dev/null
done

"$SCRIPT_DIR/seed-fs.sh"

echo "[equiv] DB reset complete: $AGITEAMAPP_EQUIV_PY_DB / $AGITEAMAPP_EQUIV_RS_DB"
