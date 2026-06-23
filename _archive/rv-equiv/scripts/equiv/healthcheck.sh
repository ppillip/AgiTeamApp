#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$SCRIPT_DIR/env.sh"

py_url="http://127.0.0.1:${AGITEAMAPP_EQUIV_PY_PORT}/healthz"
rs_url="http://127.0.0.1:${AGITEAMAPP_EQUIV_RS_PORT}/api/webgui/projects"

wait_http() {
  local name="$1"
  local url="$2"
  for _ in $(seq 1 90); do
    if curl -fsS "$url" >/tmp/agiteamapp-equiv-${name}.json 2>/dev/null; then
      echo "[equiv] $name ok: $url"
      cat "/tmp/agiteamapp-equiv-${name}.json"
      echo
      return 0
    fi
    sleep 1
  done
  echo "[equiv] $name failed: $url" >&2
  compose logs --tail=120 "${name}-backend" >&2 || true
  return 1
}

wait_http python "$py_url"
wait_http rust "$rs_url"
"$SCRIPT_DIR/warmup.sh"
echo "[equiv] healthcheck complete"
