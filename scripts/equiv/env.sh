#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$APP_DIR/docker-compose.equiv.yml"
ENV_FILE="$APP_DIR/.env.equiv"

set -a
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
elif [[ -f "$APP_DIR/.env.equiv.example" ]]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/.env.equiv.example"
fi
set +a

export POSTGRES_DB="${POSTGRES_DB:-agiteamapp}"
export POSTGRES_USER="${POSTGRES_USER:-agiteamapp}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-change-me-equiv-local-only}"
export AGITEAMAPP_EQUIV_DB_PORT="${AGITEAMAPP_EQUIV_DB_PORT:-15433}"
export AGITEAMAPP_EQUIV_PY_DB="${AGITEAMAPP_EQUIV_PY_DB:-agiteamapp_equiv_py}"
export AGITEAMAPP_EQUIV_RS_DB="${AGITEAMAPP_EQUIV_RS_DB:-agiteamapp_equiv_rs}"
export AGITEAMAPP_EQUIV_TEMPLATE_DB="${AGITEAMAPP_EQUIV_TEMPLATE_DB:-agiteamapp_equiv_template}"
export AGITEAMAPP_EQUIV_PY_PORT="${AGITEAMAPP_EQUIV_PY_PORT:-18080}"
export AGITEAMAPP_EQUIV_RS_PORT="${AGITEAMAPP_EQUIV_RS_PORT:-18081}"
export AGITEAMAPP_EQUIV_FAKE_MUX_LOG="${AGITEAMAPP_EQUIV_FAKE_MUX_LOG:-/tmp/agiteamapp-equiv/fake-mux.jsonl}"

compose() {
  docker compose --env-file "$APP_DIR/.env.equiv.example" -f "$COMPOSE_FILE" "$@"
}
