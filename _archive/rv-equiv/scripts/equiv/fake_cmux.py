#!/usr/bin/env python3
"""Fake cmux binary for AgiTeamApp parity tests.

It records attempted mux operations as JSONL and returns deterministic output.
No real cmux workspace or surface is touched.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _log_path() -> Path:
    return Path(os.environ.get("AGITEAMAPP_EQUIV_FAKE_MUX_LOG", "/tmp/agiteamapp-equiv/fake-mux.jsonl"))


def _record(argv: list[str]) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "argv": argv,
        "cwd": os.getcwd(),
        "env": {
            key: os.environ.get(key)
            for key in (
                "WEBGUI_PROJECT_ID",
                "AGITEAMAPP_PROJECT_ID",
                "CMUX_WORKSPACE_ID",
                "CMUX_SURFACE_ID",
            )
            if os.environ.get(key)
        },
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _tree() -> str:
    return """window window:1 [current]
├── workspace workspace:equiv "Panthea" [selected] ◀ active
│   ├── pane pane:equiv-1
│   │   └── surface surface:equiv-pm [terminal] "제우스(PM)" [selected] ◀ active
│   ├── pane pane:equiv-2
│   │   └── surface surface:equiv-qa [terminal] "아르고스(QA)"
"""


def main() -> int:
    argv = sys.argv[1:]
    _record(argv)
    cmd = argv[0] if argv else ""

    if cmd in {"tree", "list"}:
        print(_tree(), end="")
        return 0
    if cmd in {"read-screen", "read"}:
        print("FAKE_CMUX_SCREEN")
        return 0
    if cmd in {"send", "send-key", "key", "workspace"}:
        return 0
    if cmd in {"--version", "version"}:
        print("fake-cmux 0.1")
        return 0

    print(f"fake_cmux: accepted command {cmd}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
