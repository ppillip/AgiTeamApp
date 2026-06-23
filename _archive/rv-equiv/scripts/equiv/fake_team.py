#!/usr/bin/env python3
"""Fake team CLI for Rust cmux-mode parity tests.

Rust's cmux adapter shells out to `team read/send`. This shim records those
calls into the same JSONL log as fake_cmux.py and returns deterministic success.
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
        "argv": ["team", *argv],
        "cwd": os.getcwd(),
        "env": {
            key: os.environ.get(key)
            for key in ("AGITEAM_HOME", "AGITEAM_MUX", "AGITEAMAPP_PROJECT_ID")
            if os.environ.get(key)
        },
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    argv = sys.argv[1:]
    _record(argv)
    cmd = argv[0] if argv else ""
    if cmd == "read":
        print("FAKE_TEAM_SCREEN")
        return 0
    if cmd in {"send", "key"}:
        return 0
    if cmd in {"--help", "help"}:
        print("fake team for equiv parity")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
