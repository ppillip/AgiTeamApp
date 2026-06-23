#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$SCRIPT_DIR/env.sh"

"$SCRIPT_DIR/db-reset.sh"

echo "[equiv] clearing fake mux log"
compose exec -T equiv-db sh -c 'rm -f /tmp/agiteamapp-equiv/fake-mux.jsonl 2>/dev/null || true'

echo "[equiv] starting Python and Rust backends"
compose up -d --build python-backend rust-backend

echo "[equiv] warming fake discovery registry"
"$SCRIPT_DIR/warmup.sh"

echo "[equiv] services ready for parity capture."
