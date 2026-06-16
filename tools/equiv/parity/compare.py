"""canonical-diff 엔진.

Python(oracle)과 Rust 캡처를 RV-10 비교정책(strict/normalize/exclude)에 따라
필드별로 비교한다. 판정 주체는 사람이 아니라 이 엔진이다.

절차
1. 두 문서를 각각 normalize() — 비결정 요소를 참조 일관 placeholder로 치환
2. 트리를 동시 walk:
   - exclude 경로: 양쪽에서 제외(노출 보안 위반은 stats로 별도 탐지)
   - normalize/strict 경로: 정규화된 값으로 비교(정규화로 가려지지 않은 차이는 FAIL)
3. array_sort 경로의 배열은 비교 전 canonical 정렬(WS/poller 시간의존 경로 대응)
4. diff 목록과 PASS/FAIL 산출

용어: '정책 경로'는 배열 인덱스를 `[]`로 일반화한 형태(rules 매칭용),
'실제 경로'는 인덱스를 포함한 형태(리포트용).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonicalize, canonical_dumps, maybe_parse_json
from .normalizer import normalize, NormalizeStats
from .rules import CompareRules, Policy

_MISSING = object()

# placeholder 번호 마스크: <UUID#3> → <UUID>, <TS#5> → <TS> (cursor 등 부분 포함도)
_PH_NUM_RE = re.compile(r"<([A-Z_]+)#\d+>")


def _strip_ph(value: Any) -> Any:
    """placeholder 의 일련번호를 제거해 형식 마스크로 환원한다.

    normalize 정책 경로에서 두 문서의 비결정 요소 개수·등장순서가 달라 placeholder
    번호가 어긋나는 false FAIL 을 흡수한다(RV-55 §3.1: normalize = 형식·참조 일관성,
    문서 간 절대 번호는 비교 대상 아님). str 이 아니면 원본 그대로 반환.
    """
    if isinstance(value, str):
        return _PH_NUM_RE.sub(r"<\1>", value)
    return value


@dataclass
class Diff:
    path: str            # 실제 경로(인덱스 포함)
    policy: str          # 적용된 정책
    py: Any              # Python 측 값(없으면 "<MISSING>")
    rust: Any            # Rust 측 값(없으면 "<MISSING>")
    kind: str            # value_mismatch | missing_in_rust | missing_in_python |
                         # type_mismatch | array_length | key_set

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "policy": self.policy,
            "kind": self.kind,
            "python": self.py,
            "rust": self.rust,
        }


@dataclass
class ParityResult:
    contract_id: str | None
    diffs: list[Diff] = field(default_factory=list)
    security_violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    py_stats: NormalizeStats | None = None
    rust_stats: NormalizeStats | None = None

    @property
    def strict_diffs(self) -> list[Diff]:
        return [d for d in self.diffs if d.policy == Policy.STRICT.value]

    @property
    def passed(self) -> bool:
        # strict diff 0건 + 보안위반 0건이면 PASS.
        # normalize 경로 diff도 "정규화 후에도 다름"이므로 FAIL 처리한다.
        # warnings(abs_path 노출 등)는 RV-50 보안리뷰 신호일 뿐 parity FAIL 아님.
        return not self.diffs and not self.security_violations

    def summary(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "passed": self.passed,
            "diff_count": len(self.diffs),
            "strict_diff_count": len(self.strict_diffs),
            "security_violations": self.security_violations,
            "warnings": self.warnings,
            "diffs": [d.as_dict() for d in self.diffs],
        }


# mux 캡처 중 discovery/liveness 백그라운드 명령은 폴링 타이밍에 의존하는 비결정 호출이라
# 양쪽 캡처에 비대칭으로 찍힌다(false FAIL). 명시적 side-effect(send/key submit)만 비교한다.
_DISCOVERY_MUX_CMDS = {"tree", "read-screen", "read", "list", "rpc", "status", "version"}


def _filter_mux_discovery(doc: Any) -> Any:
    """doc['mux'] 배열에서 discovery/liveness 명령 요소를 제거한다(비결정 제외)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("mux"), list):
        return doc
    kept = []
    for ev in doc["mux"]:
        argv = ev.get("argv") if isinstance(ev, dict) else None
        cmd = argv[0] if isinstance(argv, list) and argv else None
        sub = argv[1] if isinstance(argv, list) and len(argv) > 1 else None
        # discovery/liveness 명령 또는 read-only(team read/read-screen/list) 캡처는
        # 메시지 전달에 무영향인 서비스 노이즈 → 비교 제외(send/key side-effect 만 비교).
        if cmd in _DISCOVERY_MUX_CMDS:
            continue
        if cmd == "team" and sub in ("read", "read-screen", "list", "tree"):
            continue
        kept.append(ev)
    out = dict(doc)
    # discovery 제거 후 남은 side-effect 가 없으면 mux 키 자체를 제거한다
    # (한쪽 [] vs 한쪽 키없음 의 false diff 방지 — discovery-only 캡처는 비교 대상 아님).
    if kept:
        out["mux"] = kept
    else:
        out.pop("mux", None)
    return out


def compare_documents(
    py_doc: Any,
    rust_doc: Any,
    rules: CompareRules,
    contract_id: str | None = None,
    array_sort_paths: set[str] | None = None,
) -> ParityResult:
    """두 캡처 문서를 비교해 ParityResult를 반환한다."""
    array_sort_paths = array_sort_paths or set()

    py_norm, py_stats = normalize(_pre(_filter_mux_discovery(py_doc)))
    rust_norm, rust_stats = normalize(_pre(_filter_mux_discovery(rust_doc)))

    result = ParityResult(
        contract_id=contract_id,
        py_stats=py_stats,
        rust_stats=rust_stats,
    )

    # host absolute path 노출은 placeholder로 정규화돼 parity 비교에는 영향 없다.
    # 다만 TS-11 §3.1상 "공개 응답 노출 금지" 신호이므로 경고로 남긴다(RV-50 보안리뷰용).
    if py_stats.abs_path_hits:
        result.warnings.append(
            f"python 응답에 host absolute path 노출 {len(py_stats.abs_path_hits)}건 (RV-50 검토)"
        )
    if rust_stats.abs_path_hits:
        result.warnings.append(
            f"rust 응답에 host absolute path 노출 {len(rust_stats.abs_path_hits)}건 (RV-50 검토)"
        )

    # 공개금지 키 노출은 parity FAIL이다(§5.3: role_id, raw secret/payload 노출 시 FAIL).
    # 단 "공개 응답(HTTP body·WS 이벤트)" 한정이다. DB snapshot에는 role_id·payload_json
    # 컬럼이 당연히 존재(내부 저장)하므로 검사 대상에서 제외한다(과잉탐지 방지).
    forbidden = set(rules.meta.get("forbidden_keys", []))
    if forbidden:
        for backend, doc in (("python", py_doc), ("rust", rust_doc)):
            scope = _public_scope(doc)
            hits = _find_forbidden_keys(scope, forbidden)
            for h in hits:
                result.security_violations.append(f"{backend}: 공개 응답에 금지 키 노출 '{h}'")

    _compare(
        py_norm, rust_norm,
        actual_path="", policy_path="",
        rules=rules, contract_id=contract_id,
        array_sort_paths=array_sort_paths, out=result,
    )
    return result


def _public_scope(doc: Any) -> Any:
    """공개 응답 영역(http.body, ws_events, mux)만 추출. db는 내부 저장이라 제외."""
    if not isinstance(doc, dict):
        return doc
    scope: dict[str, Any] = {}
    if isinstance(doc.get("http"), dict) and "body" in doc["http"]:
        scope["body"] = doc["http"]["body"]
    if "ws_events" in doc:
        scope["ws_events"] = doc["ws_events"]
    if "mux" in doc:
        scope["mux"] = doc["mux"]
    return scope


def _find_forbidden_keys(value: Any, forbidden: set[str], found: set[str] | None = None) -> set[str]:
    """문서 전체에서 공개금지 키(dict 키)의 존재를 탐지한다."""
    if found is None:
        found = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k in forbidden:
                found.add(k)
            _find_forbidden_keys(v, forbidden, found)
    elif isinstance(value, list):
        for v in value:
            _find_forbidden_keys(v, forbidden, found)
    return found


def _pre(doc: Any) -> Any:
    """비교 전 전처리: JSONB 문자열 파싱(키순서 비결정 제거)."""
    return _walk_parse(doc)


def _walk_parse(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _walk_parse(maybe_parse_json(val)) for k, val in v.items()}
    if isinstance(v, list):
        return [_walk_parse(maybe_parse_json(val)) for val in v]
    return v


def _compare(
    py: Any, rust: Any,
    actual_path: str, policy_path: str,
    rules: CompareRules, contract_id: str | None,
    array_sort_paths: set[str], out: ParityResult,
) -> None:
    policy = rules.policy_for(policy_path, contract_id) if policy_path else Policy.STRICT

    if policy == Policy.EXCLUDE:
        return  # 양쪽 모두 비교 제외

    # 한쪽 누락
    if py is _MISSING:
        out.diffs.append(Diff(actual_path, policy.value, "<MISSING>", rust, "missing_in_python"))
        return
    if rust is _MISSING:
        out.diffs.append(Diff(actual_path, policy.value, py, "<MISSING>", "missing_in_rust"))
        return

    # 타입 불일치
    if type(py) is not type(rust) and not _both_number(py, rust):
        out.diffs.append(Diff(actual_path, policy.value, py, rust, "type_mismatch"))
        return

    if isinstance(py, dict):
        keys = set(py.keys()) | set(rust.keys())
        for k in sorted(keys):
            ap = f"{actual_path}.{k}" if actual_path else k
            pp = f"{policy_path}.{k}" if policy_path else k
            _compare(
                py.get(k, _MISSING), rust.get(k, _MISSING),
                ap, pp, rules, contract_id, array_sort_paths, out,
            )
        return

    if isinstance(py, list):
        # 정렬키 sort 경로(시간의존 배열): canonical 사전순으로 정렬 후 비교.
        # 경로 표기는 배열 요소 형태(`events[]`)와 배열 자체(`events`) 둘 다 허용.
        pa, ra = py, rust
        if policy_path in array_sort_paths or f"{policy_path}[]" in array_sort_paths:
            pa = sorted(py, key=canonical_dumps)
            ra = sorted(rust, key=canonical_dumps)
        if len(pa) != len(ra):
            out.diffs.append(Diff(actual_path, policy.value, len(pa), len(ra), "array_length"))
            # 길이가 달라도 가능한 만큼 인덱스 비교(원인 파악용)
        for i in range(max(len(pa), len(ra))):
            ap = f"{actual_path}[{i}]"
            pp = f"{policy_path}[]"
            _compare(
                pa[i] if i < len(pa) else _MISSING,
                ra[i] if i < len(ra) else _MISSING,
                ap, pp, rules, contract_id, array_sort_paths, out,
            )
        return

    # scalar
    if _both_number(py, rust):
        if float(py) != float(rust):
            out.diffs.append(Diff(actual_path, policy.value, py, rust, "value_mismatch"))
        return
    if py != rust:
        # normalize 경로: placeholder 번호(<UUID#n>/<TS#n>)는 문서 간 어긋날 수 있으므로
        # 형식 마스크로 환원해 재비교(번호차는 흡수, 형식차는 여전히 FAIL).
        if policy == Policy.NORMALIZE and _strip_ph(py) == _strip_ph(rust):
            return
        out.diffs.append(Diff(actual_path, policy.value, py, rust, "value_mismatch"))


def _both_number(a: Any, b: Any) -> bool:
    num = (int, float)
    return isinstance(a, num) and isinstance(b, num) and not isinstance(a, bool) and not isinstance(b, bool)
