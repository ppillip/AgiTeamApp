#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/tmp/agiteamapp-front.log"
BG=0

usage() {
  cat <<'EOF'
Usage: ./startfront.sh [--bg]

Starts the AgiTeamApp Vite frontend on port 1420.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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

echo "[frontend] stopping process listening on tcp:1420, if any"
if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti tcp:1420 || true)"
  if [ -n "$pids" ]; then
    printf '%s\n' "$pids" | xargs kill
  fi
else
  echo "[frontend] lsof not found; cannot check tcp:1420" >&2
fi

cd "$APP_ROOT"

export VITE_API_PROXY="${VITE_API_PROXY:-http://127.0.0.1:8000}"

if [ "$BG" -eq 1 ]; then
  echo "[frontend] starting in background; log: $LOG_FILE"
  nohup npx vite --host 0.0.0.0 --port 1420 > "$LOG_FILE" 2>&1 &
  echo "[frontend] pid: $!"
else
  echo "[frontend] starting in foreground on port 1420"
  exec npx vite --host 0.0.0.0 --port 1420
fi
