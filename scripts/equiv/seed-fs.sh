#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$SCRIPT_DIR/env.sh"

PROJECT_ROOT="${WEBGUI_PROJECT_ROOTS_JSON:-}"
if [[ "$PROJECT_ROOT" == *'"/projects/Panthea"'* ]]; then
  # Host path equivalent for the compose mount `../..:/projects/Panthea:ro`.
  PROJECT_ROOT="$(cd "$APP_DIR/../.." && pwd)"
else
  PROJECT_ROOT="$(cd "$APP_DIR/../.." && pwd)"
fi

UPLOAD_DIR="$PROJECT_ROOT/.agiteam/webgui/uploads/images"
mkdir -p "$UPLOAD_DIR"

python3 - "$UPLOAD_DIR" <<'PY'
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

upload_dir = Path(sys.argv[1])
attachment_id = "att_0000000000004000800000000000601"
filename = "upload-20260616T090000Z-rv60.png"

# 1x1 transparent PNG.
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB"
    "/6X6n9sAAAAASUVORK5CYII="
)
(upload_dir / filename).write_bytes(png)
(upload_dir / f"{attachment_id}.json").write_text(
    json.dumps(
        {
            "attachment_id": attachment_id,
            "client_attachment_id": "rv40-image-001",
            "project_id": "Panthea",
            "kind": "image",
            "filename": filename,
            "mime_type": "image/png",
            "size_bytes": len(png),
            "width": 1,
            "height": 1,
            "sha256": "seed-rv60-preview",
            "created_at": "2026-06-16T09:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        },
        ensure_ascii=False,
        sort_keys=True,
    ),
    encoding="utf-8",
)
PY

echo "[equiv] filesystem seed complete: $UPLOAD_DIR"
