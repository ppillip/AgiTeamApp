#!/usr/bin/env python3
"""RV-60 parity runner.

Runs the RV-40 representative corpus against the already-running RV-20
environment:

- Python oracle: http://127.0.0.1:18080
- Rust target:   http://127.0.0.1:18081
- DBs: agiteamapp_equiv_py / agiteamapp_equiv_rs

Unlike run_rv50.py this runner covers multipart and WebSocket cases, resets
each DB back to the deterministic RV-60 seed before each backend capture, and
captures DB/mux side effects for the contracts whose rules define them.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from parity.compare import compare_documents  # noqa: E402
from parity.fixtures import Capture, load_case  # noqa: E402
from parity.rules import load_rules  # noqa: E402
from parity.ws_client import WebSocketClient, WebSocketError  # noqa: E402

CASES_DIR = ROOT / "cases" / "RV-40"
RULES_PATH = ROOT / "rules" / "compare-rules.rv40.json"
CAP_DIR = ROOT / "captures" / "RV-60"
REPORT_DIR = ROOT / "reports"

PY_BASE = "http://127.0.0.1:18080"
RS_BASE = "http://127.0.0.1:18081"
PY_WS = "ws://127.0.0.1:18080"
RS_WS = "ws://127.0.0.1:18081"

DB_CONTAINER = "agiteamapp-equiv-db"
PY_DB = "agiteamapp_equiv_py"
RS_DB = "agiteamapp_equiv_rs"
MUX_LOG = "/tmp/agiteamapp-equiv/fake-mux.jsonl"

HEADERS_DROP = {"authorization"}  # equiv env has auth disabled.

DB_SNAPSHOT = {
    "WG-PROJ-01": [("webgui_room", "project_id, role_id")],
    "WG-MSG-02": [("webgui_message", "recorded_at, message_id")],
    "WG-MSG-06": [("webgui_message", "recorded_at, message_id")],
    "WG-CHAT-03": [("webgui_room", "project_id, role_id")],
    "WG-CHAT-05": [("webgui_message", "recorded_at, message_id")],
    "WG-CHAT-06": [("webgui_runtime_event", "recorded_at, event_id")],
    "WG-HOOK-01": [
        ("webgui_room", "project_id, role_id"),
        ("webgui_message", "recorded_at, message_id"),
        ("webgui_runtime_event", "recorded_at, event_id"),
    ],
    "WG-ACT-01": [
        ("webgui_message", "recorded_at, message_id"),
        ("webgui_runtime_event", "recorded_at, event_id"),
    ],
}

ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB"
    "/6X6n9sAAAAASUVORK5CYII="
)


def _run(cmd: list[str], *, timeout: int = 20, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)


def _reset_db(db: str) -> None:
    seed_sql = Path("system/AgiTeamApp/scripts/equiv/seed.sql").read_text(encoding="utf-8")
    seed_body = "\n".join(
        line
        for line in seed_sql.splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )
    sql = f"""
BEGIN;
TRUNCATE webgui_runtime_event, webgui_message, webgui_agent_session, webgui_room RESTART IDENTITY CASCADE;
{seed_body}
COMMIT;
"""
    out = _run(
        [
            "docker",
            "exec",
            "-i",
            DB_CONTAINER,
            "psql",
            "-U",
            "agiteamapp",
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input_text=sql,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"DB reset failed for {db}: {out.stderr or out.stdout}")
    out = _run(
        [
            "docker",
            "exec",
            DB_CONTAINER,
            "psql",
            "-U",
            "agiteamapp",
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "SELECT 1;",
        ],
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"DB ping failed for {db}: {out.stderr or out.stdout}")


def _reset_all() -> None:
    _reset_db(PY_DB)
    _reset_db(RS_DB)


def _query_string(query: dict) -> str:
    q = {k: ("true" if v is True else "false" if v is False else v) for k, v in query.items() if v is not None}
    return urlencode(q)


def _url(base: str, req: dict) -> str:
    path = req["path"]
    query = req.get("query") or {}
    if query:
        path = f"{path}?{_query_string(query)}"
    return base + path


def _headers(req: dict) -> dict[str, str]:
    return {k: v for k, v in (req.get("headers") or {}).items() if k.lower() not in HEADERS_DROP}


def _multipart_body(parts: dict) -> tuple[bytes, str]:
    boundary = "----agiteamapp-rv60-boundary"
    chunks: list[bytes] = []
    for name, value in parts.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        if name == "file":
            fixture = ROOT / str(value)
            data = fixture.read_bytes() if fixture.exists() else ONE_BY_ONE_PNG
            filename = Path(str(value)).name
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    "Content-Type: image/png\r\n\r\n"
                ).encode("utf-8")
            )
            chunks.append(data)
            chunks.append(b"\r\n")
        else:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _http_capture(base: str, case, backend: str) -> Capture:
    req = case.request
    url = _url(base, req)
    headers = _headers(req)
    body = req.get("body")
    data = None
    if isinstance(body, dict) and "multipart" in body:
        data, ctype = _multipart_body(body["multipart"])
        headers["content-type"] = ctype
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=req.get("method", "GET"))
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        raw = e.read()
    except urllib.error.URLError as e:
        return Capture(case_id=case.case_id, backend=backend, http={"status": 0, "error": f"URLError: {e.reason}"})

    ctype = resp_headers.get("content-type", "")
    http: dict = {"status": status, "headers": _keep_headers(resp_headers)}
    if "application/json" in ctype and raw:
        try:
            http["body"] = json.loads(raw)
        except ValueError:
            http["body"] = {"_unparseable": raw[:200].decode("utf-8", "replace")}
    elif raw:
        http["body_b64"] = base64.b64encode(raw).decode("ascii")
    else:
        http["body"] = None
    return Capture(case_id=case.case_id, backend=backend, http=http)


def _keep_headers(headers: dict[str, str]) -> dict[str, str]:
    keep = {
        "content-type",
        "content-length",
        "accept-ranges",
        "content-range",
        "content-security-policy",
        "x-content-type-options",
        "content-disposition",
    }
    return {k: v for k, v in headers.items() if k in keep}


def _db_rows(db: str, table: str, order: str) -> list:
    sql = f"select to_jsonb(t) from (select * from {table} order by {order}) t;"
    out = _run(
        ["docker", "exec", DB_CONTAINER, "psql", "-U", "agiteamapp", "-d", db, "-t", "-A", "-c", sql],
        timeout=15,
    )
    if out.returncode != 0:
        return [{"_db_error": out.stderr.strip() or out.stdout.strip()}]
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append({"_unparseable": line})
    return rows


def _augment_db(cap: Capture, contract_id: str, db: str) -> None:
    specs = DB_SNAPSHOT.get(contract_id)
    if specs:
        cap.db = {tbl: _db_rows(db, tbl, order) for tbl, order in specs}


def _clear_mux() -> None:
    _run(["docker", "exec", DB_CONTAINER, "sh", "-c", f"rm -f {MUX_LOG}"], timeout=10)


def _read_mux() -> list:
    out = _run(["docker", "exec", DB_CONTAINER, "sh", "-c", f"cat {MUX_LOG} 2>/dev/null || true"], timeout=10)
    rows = []
    for line in out.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _capture_backend(base: str, ws_base: str, case, backend: str, db: str, cases_by_id: dict[str, object]) -> Capture:
    _reset_db(db)
    _clear_mux()
    if case.request.get("kind") == "ws":
        cap = _ws_capture(ws_base, base, case, backend, cases_by_id)
    else:
        cap = _http_capture(base, case, backend)
    _augment_db(cap, case.contract_id, db)
    mux = _read_mux()
    if mux:
        cap.mux = mux
    return cap


def _ws_url(ws_base: str, req: dict) -> str:
    path = req["path"]
    query = req.get("query") or {}
    if query:
        path = f"{path}?{_query_string(query)}"
    return ws_base + path


def _wait_for_event(client: WebSocketClient, expected_type: str | None, timeout: float = 5.0) -> list[dict]:
    deadline = time.time() + timeout
    events = []
    while time.time() < deadline:
        try:
            ev = client.recv_event().comparable()
        except (socket.timeout, WebSocketError):
            break
        events.append(ev)
        if expected_type is None or ev.get("type") == expected_type:
            break
    return events


def _ws_capture(ws_base: str, http_base: str, case, backend: str, cases_by_id: dict[str, object]) -> Capture:
    url = _ws_url(ws_base, case.request)
    events: list[dict] = []
    try:
        with WebSocketClient(url, headers=_headers(case.request), timeout=5.0) as client:
            for op in case.request.get("ws_ops", []):
                kind = op.get("op")
                if kind == "connect":
                    continue
                if kind == "expect_replay":
                    count = int(op.get("count_min", 1))
                    deadline = time.time() + 5
                    while len(events) < count and time.time() < deadline:
                        try:
                            events.append(client.recv_event().comparable())
                        except socket.timeout:
                            break
                elif kind == "trigger_http_case":
                    trigger = cases_by_id[op["case_id"]]
                    _http_capture(http_base, trigger, backend)
                elif kind == "expect_event":
                    events.extend(_wait_for_event(client, op.get("type"), timeout=5.0))
                elif kind == "send_json":
                    client.send_json(op.get("payload"))
    except Exception as e:  # noqa: BLE001
        events.append({"type": "client_error", "error": str(e)})
    return Capture(case_id=case.case_id, backend=backend, ws_events=events)


def _save_capture(cap: Capture, suffix: str) -> Path:
    path = CAP_DIR / f"{cap.case_id}.{suffix}.json"
    path.write_text(json.dumps(cap.comparable(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    rules = load_rules(RULES_PATH)
    CAP_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cases = [load_case(p) for p in sorted(CASES_DIR.glob("*.case.json"))]
    cases_by_id = {c.case_id: c for c in cases}

    results = []
    for case in cases:
        py_cap = _capture_backend(PY_BASE, PY_WS, case, "python", PY_DB, cases_by_id)
        rs_cap = _capture_backend(RS_BASE, RS_WS, case, "rust", RS_DB, cases_by_id)
        _save_capture(py_cap, "python.golden")
        _save_capture(rs_cap, "rust.actual")

        res = compare_documents(
            py_cap.comparable(),
            rs_cap.comparable(),
            rules,
            contract_id=case.contract_id,
            array_sort_paths=set(case.array_sort_paths),
        )
        summary = res.summary()
        summary["case_id"] = case.case_id
        summary["result"] = "PASS" if res.passed else "FAIL"
        summary["py_status"] = (py_cap.http or {}).get("status")
        summary["rs_status"] = (rs_cap.http or {}).get("status")
        summary["py_ws_events"] = len(py_cap.ws_events or [])
        summary["rs_ws_events"] = len(rs_cap.ws_events or [])
        results.append(summary)

        st = f"{summary.get('py_status')}/{summary.get('rs_status')}" if py_cap.http or rs_cap.http else "ws"
        print(f"{case.contract_id:<14}{st:<14}{summary['result']:<8}diffs={summary['diff_count']}")

    report = {
        "doc": "RV-60 parity full-run result",
        "py_base": PY_BASE,
        "rs_base": RS_BASE,
        "total": len(results),
        "pass": sum(1 for r in results if r["result"] == "PASS"),
        "fail": sum(1 for r in results if r["result"] == "FAIL"),
        "skip": 0,
        "results": results,
    }
    report_path = REPORT_DIR / "rv60-parity.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS={report['pass']} FAIL={report['fail']} SKIP=0 / {report['total']}")
    print(f"리포트: {report_path}")
    return 0 if report["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
