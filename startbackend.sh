#!/usr/bin/env bash
set -euo pipefail

# Backend = single central kernel receiver. Project-local teamwatch edges push transcripts to /api/webgui/internal/messages/collect.
# The backend does not read transcript files or run transcript polling.

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${APP_ROOT}/backend-rs"
LOG_FILE="/tmp/agiteamapp-rs.log"
BUILD=0
BG=0

usage() {
  cat <<'EOF'
Usage: ./startbackend.sh [--build] [--bg]

Starts the AgiTeamApp Rust backend.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build)
      BUILD=1
      shift
      ;;
    --bg)
      BG=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

echo "[backend] stopping existing agiteamapp-http process, if any"
pkill -f 'target/release/agiteamapp-http' || true

if [ "$BUILD" -eq 1 ]; then
  echo "[backend] building release binary"
  (cd "$BACKEND_DIR" && cargo build --release -p agiteamapp-http)
fi

cd "$BACKEND_DIR"

export AGITEAMAPP_DATABASE_URL="${AGITEAMAPP_DATABASE_URL:-postgres://agiteamapp:agiteamapp_dev_pw@127.0.0.1:15432/agiteamapp}"
export AGITEAMAPP_MUX="${AGITEAMAPP_MUX:-team}"
export AGITEAMAPP_PROJECT_ID="${AGITEAMAPP_PROJECT_ID:-Panthea}"
export AGITEAMAPP_PROJECTS_BASE="${AGITEAMAPP_PROJECTS_BASE:-/Users/ppillip/Projects}"
export AGITEAMAPP_RS_PORT="${AGITEAMAPP_RS_PORT:-8000}"

if [ "$BG" -eq 1 ]; then
  echo "[backend] starting in background; log: $LOG_FILE"
  nohup ./target/release/agiteamapp-http > "$LOG_FILE" 2>&1 &
  echo "[backend] pid: $!"
else
  echo "[backend] starting in foreground on port ${AGITEAMAPP_RS_PORT}"
  exec ./target/release/agiteamapp-http
fi
