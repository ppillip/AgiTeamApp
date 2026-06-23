#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
source "$SCRIPT_DIR/env.sh"

python3 - "$AGITEAMAPP_EQUIV_PY_PORT" "$AGITEAMAPP_EQUIV_RS_PORT" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

py_port, rs_port = sys.argv[1], sys.argv[2]
targets = {
    "python": f"http://127.0.0.1:{py_port}",
    "rust": f"http://127.0.0.1:{rs_port}",
}


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as res:
        if res.status != 200:
            raise RuntimeError(f"{url} returned HTTP {res.status}")
        return json.loads(res.read().decode("utf-8"))


def normalize_roles(roles) -> list[str]:
    out: list[str] = []
    for role in roles or []:
        if isinstance(role, str):
            out.append(role)
        elif isinstance(role, dict):
            value = role.get("role") or role.get("role_id")
            if value:
                out.append(str(value))
    order = {"PM": 0, "Architect": 1, "DeveloperBE": 2, "DeveloperFE": 3, "Designer": 4, "QA": 5, "DevOps": 6}
    return sorted(dict.fromkeys(out), key=lambda r: order.get(r, 99))


def project_summary(payload: dict) -> dict:
    data = payload.get("data") or {}
    projects = data.get("projects") or []
    panthea = next((p for p in projects if p.get("project_id") == "Panthea"), None)
    if not isinstance(panthea, dict):
        raise AssertionError("Panthea project not found")
    return {
        "selected_project_id": data.get("selected_project_id"),
        "project_id": panthea.get("project_id"),
        "workspace_id": panthea.get("workspace_id"),
        "workspace_title": panthea.get("workspace_title"),
        "connection_state": panthea.get("connection_state"),
        "pm_connection_state": panthea.get("pm_connection_state"),
        "room_count": panthea.get("room_count"),
        "roles": normalize_roles(panthea.get("roles")),
    }


def ready(summary: dict) -> bool:
    return summary == {
        "selected_project_id": "Panthea",
        "project_id": "Panthea",
        "workspace_id": "workspace:equiv",
        "workspace_title": "Panthea",
        "connection_state": "connected",
        "pm_connection_state": "connected",
        "room_count": 2,
        "roles": ["PM", "QA"],
    }


deadline = time.monotonic() + 90
last: dict[str, object] = {}
while time.monotonic() < deadline:
    try:
        summaries = {}
        for name, base in targets.items():
            fetch_json(f"{base}/healthz")
            summaries[name] = project_summary(fetch_json(f"{base}/api/webgui/projects"))
        last = summaries
        if all(ready(summary) for summary in summaries.values()) and summaries["python"] == summaries["rust"]:
            print("[equiv] discovery warm-up ok")
            print(json.dumps(summaries, ensure_ascii=False, sort_keys=True))
            raise SystemExit(0)
    except (AssertionError, RuntimeError, OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        last = {"error": str(exc), "last": last}
    time.sleep(1)

print("[equiv] discovery warm-up failed", file=sys.stderr)
print(json.dumps(last, ensure_ascii=False, sort_keys=True), file=sys.stderr)
raise SystemExit(1)
PY
