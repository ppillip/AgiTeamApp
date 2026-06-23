#!/usr/bin/env python3
"""RV-50(BE분) parity 1회전 러너.

RV-40 case 23개를 Python(oracle 18080)·Rust(18081)에 던져 응답을 캡처하고,
parity.compare_documents로 계약별 PASS/FAIL을 기계 판정한다.

- HTTP만 실행한다. WS(WG-MSG-05)는 표준 라이브러리에 WS 클라이언트가 없어
  1회전에서 제외하고 사유를 리포트한다(RV-60 대상).
- seed가 비어 있으므로 placeholder(<fixture-room-id> 등)는 그대로 던진다.
  양쪽이 같은 에러/빈결과를 주는지(=parity)를 비교한다.
- collector/write 핵심 계약은 요청 후 DB snapshot을 양쪽 DB에서 조회해 보강한다.

실행: python3 tools/equiv/run_rv50.py
출력: captures/RV-50/<case>.{python.golden,rust.actual}.json, reports/rv50-parity.json
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from parity.compare import compare_documents  # noqa: E402
from parity.fixtures import Capture, load_case  # noqa: E402
from parity.rules import load_rules  # noqa: E402

CASES_DIR = ROOT / "cases" / "RV-40"
RULES_PATH = ROOT / "rules" / "compare-rules.rv40.json"
CAP_DIR = ROOT / "captures" / "RV-50"
REPORT_DIR = ROOT / "reports"

PY_BASE = "http://127.0.0.1:18080"
RS_BASE = "http://127.0.0.1:18081"

DB_CONTAINER = "agiteamapp-equiv-db"
PY_DB = "agiteamapp_equiv_py"
RS_DB = "agiteamapp_equiv_rs"

# DB snapshot을 보강할 계약 → 조회할 테이블·정렬키
DB_SNAPSHOT = {
    "WG-CHAT-05": [("webgui_message", "recorded_at")],
    "WG-CHAT-06": [("webgui_runtime_event", "recorded_at")],
    "WG-HOOK-01": [("webgui_room", "role_id"), ("webgui_runtime_event", "recorded_at")],
    "WG-ACT-01": [("webgui_message", "recorded_at"), ("webgui_runtime_event", "recorded_at")],
    "WG-CHAT-03": [("webgui_room", "role_id")],
    "WG-ART-05": [],  # 파일 부작용; DB 없음
}

HEADERS_DROP = {"authorization"}  # 토큰 미설정(인증 생략) → 요청에서 제거


def _http_capture(base: str, case, backend: str) -> Capture:
    req = case.request
    path = req["path"]
    query = req.get("query") or {}
    # 쿼리 조립(placeholder 포함값도 그대로 인코딩)
    # bool은 실제 Vue 클라이언트와 동일하게 소문자("true"/"false")로 직렬화한다.
    # (Python urlencode 기본은 "True" 대문자 → axum serde가 거부, 거짓 동작차이 유발)
    if query:
        from urllib.parse import urlencode
        q = {k: ("true" if v is True else "false" if v is False else v) for k, v in query.items()}
        path = f"{path}?{urlencode(q)}"
    url = base + path

    data = None
    headers = {k: v for k, v in (req.get("headers") or {}).items() if k.lower() not in HEADERS_DROP}
    body = req.get("body")
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"

    method = req.get("method", "GET")
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    status = None
    resp_headers: dict = {}
    parsed_body = None
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        resp_headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        raw = e.read()
    except urllib.error.URLError as e:
        return Capture(case_id=case.case_id, backend=backend,
                       http={"status": 0, "error": f"URLError: {e.reason}"})

    ctype = resp_headers.get("content-type", "")
    if "application/json" in ctype and raw:
        try:
            parsed_body = json.loads(raw)
        except ValueError:
            parsed_body = {"_unparseable": raw[:200].decode("utf-8", "replace")}
    else:
        # 바이너리/텍스트: 길이·타입만(바이트 동등성은 RV-60에서 정밀)
        parsed_body = {"_nonjson_len": len(raw)}

    # 비교에 쓰는 header subset만 보존(나머지는 rules가 exclude)
    keep = {"content-type", "content-length", "accept-ranges", "content-range",
            "content-security-policy", "x-content-type-options", "content-disposition"}
    hdr = {k: v for k, v in resp_headers.items() if k in keep}

    return Capture(case_id=case.case_id, backend=backend,
                   http={"status": status, "headers": hdr, "body": parsed_body})


def _db_rows(db: str, table: str, order: str) -> list:
    sql = f"select to_jsonb(t) from (select * from {table} order by {order}) t;"
    try:
        out = subprocess.run(
            ["docker", "exec", DB_CONTAINER, "psql", "-U", "agiteamapp", "-d", db,
             "-t", "-A", "-c", sql],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        return [{"_db_error": str(e)}]
    rows = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _augment_db(cap: Capture, contract_id: str, db: str) -> None:
    specs = DB_SNAPSHOT.get(contract_id)
    if not specs:
        return
    cap.db = {tbl: _db_rows(db, tbl, order) for tbl, order in specs}


def main() -> int:
    rules = load_rules(RULES_PATH)
    CAP_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for cf in sorted(CASES_DIR.glob("*.case.json")):
        case = load_case(cf)
        cid = case.contract_id

        # WS는 1회전 제외
        if case.request.get("kind") == "ws":
            results.append({"contract_id": cid, "case_id": case.case_id,
                            "result": "SKIP", "reason": "WS 클라이언트 부재(표준 라이브러리). RV-60 대상"})
            continue

        py_cap = _http_capture(PY_BASE, case, "python")
        rs_cap = _http_capture(RS_BASE, case, "rust")
        _augment_db(py_cap, cid, PY_DB)
        _augment_db(rs_cap, cid, RS_DB)

        # 캡처 저장(증거)
        (CAP_DIR / f"{case.case_id}.python.golden.json").write_text(
            json.dumps(py_cap.comparable(), ensure_ascii=False, indent=2), encoding="utf-8")
        (CAP_DIR / f"{case.case_id}.rust.actual.json").write_text(
            json.dumps(rs_cap.comparable(), ensure_ascii=False, indent=2), encoding="utf-8")

        res = compare_documents(
            py_cap.comparable(), rs_cap.comparable(), rules,
            contract_id=cid, array_sort_paths=set(case.array_sort_paths),
        )
        s = res.summary()
        s["case_id"] = case.case_id
        s["result"] = "PASS" if res.passed else "FAIL"
        s["py_status"] = (py_cap.http or {}).get("status")
        s["rs_status"] = (rs_cap.http or {}).get("status")
        results.append(s)

    report = {
        "doc": "RV-50(BE분) parity 1회전 결과",
        "py_base": PY_BASE, "rs_base": RS_BASE,
        "total": len(results),
        "pass": sum(1 for r in results if r.get("result") == "PASS"),
        "fail": sum(1 for r in results if r.get("result") == "FAIL"),
        "skip": sum(1 for r in results if r.get("result") == "SKIP"),
        "results": results,
    }
    (REPORT_DIR / "rv50-parity.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 콘솔 요약 표
    print(f"{'CONTRACT':<14}{'STATUS(py/rs)':<16}{'RESULT':<8}DIFF/원인")
    print("-" * 78)
    for r in results:
        if r.get("result") == "SKIP":
            print(f"{r['contract_id']:<14}{'-':<16}{'SKIP':<8}{r['reason']}")
            continue
        st = f"{r['py_status']}/{r['rs_status']}"
        head = ""
        if r["result"] == "FAIL":
            kinds = {}
            for d in r["diffs"]:
                kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
            head = f"diffs={r['diff_count']} {kinds}"
            if r["security_violations"]:
                head += f" SEC={r['security_violations']}"
        print(f"{r['contract_id']:<14}{st:<16}{r['result']:<8}{head}")
    print("-" * 78)
    print(f"PASS={report['pass']} FAIL={report['fail']} SKIP={report['skip']} / {report['total']}")
    print(f"리포트: {REPORT_DIR / 'rv50-parity.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
