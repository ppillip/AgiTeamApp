"""비교정책(strict/normalize/exclude) 로더 — RV-10 TS-11 §3 기계화.

정책 결정은 "무엇을 숨기고(exclude) 무엇을 느슨히 보고(normalize) 무엇을 엄격히
보는가(strict)"를 *명시적 설정*으로 분리한다. 이 설정은 아르고스(QA) 교차검토
대상이며, 도구 코드가 아니라 데이터(JSON)로 관리한다.

우선순위(구체적인 것이 이긴다):
1. 계약별 path 규칙(rules["contracts"][cid]["fields"])
2. 공통 path 규칙(rules["common"]["fields"])
3. 필드명 휴리스틱(TS-11 §3.1) — 키 이름 기반 기본값
4. 기본 strict

path 표기(간단한 glob):
- 점(.)으로 단계 구분, 배열은 `[]`(임의 인덱스) 또는 `[n]`
- `**` 는 임의 깊이 와일드카드
예) `data.rooms[].room_id`, `**.created_at`, `headers.date`
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Policy(str, Enum):
    STRICT = "strict"
    NORMALIZE = "normalize"
    EXCLUDE = "exclude"


# TS-11 §3.1 공통 필드 3분류를 키 이름 휴리스틱으로 옮긴 기본값.
# 명시 규칙이 없을 때만 적용된다. 계약별 rules로 언제든 override 가능.
_HEURISTIC_NORMALIZE_SUFFIX = (
    "_id",        # generated UUID 류 (room_id, message_id, event_id, ...)
    "_at",        # timestamp (created_at, updated_at, last_message_at, ...)
)
_HEURISTIC_NORMALIZE_EXACT = {
    "id",
    "cursor",
    "next_cursor",
    "prev_cursor",
    "server_time",
    "last_active_at",
    "occurred_at",
    "recorded_at",
    "discovered_at",
    "started_at",
    "last_seen_at",
    "ended_at",
}
# created_at 은 §5.2에서 일부 테이블 exact 지정이 있으나, 공통 기본은 normalize.
# strict로 되돌릴 곳은 계약 rules에서 명시한다.

_HEURISTIC_EXCLUDE_EXACT = {
    "date", "server", "connection", "transfer-encoding",  # HTTP transport
    "raw_text", "raw_payload", "raw_stdin", "hook_stdin",  # raw secret/payload
    "payload_json",                                        # raw (masked_payload_json만 strict)
}


@dataclass
class _Rule:
    pattern: str
    policy: Policy
    regex: re.Pattern = field(init=False)
    specificity: int = field(init=False)

    def __post_init__(self) -> None:
        self.regex = _compile_path(self.pattern)
        # 구체성: '**'가 적을수록, 세그먼트가 많을수록 구체적
        segs = self.pattern.split(".")
        self.specificity = len(segs) * 10 - self.pattern.count("**") * 5


@dataclass
class CompareRules:
    common_rules: list[_Rule]
    contract_rules: dict[str, list[_Rule]]
    # DB snapshot 정렬·키 설정: {contract_id: {table: {"sort_keys": [...], ...}}}
    db_snapshot: dict[str, Any]
    meta: dict[str, Any]

    def policy_for(self, path: str, contract_id: str | None = None) -> Policy:
        """주어진 경로의 정책을 결정한다(우선순위 적용)."""
        # 1. 계약별 path 규칙
        if contract_id and contract_id in self.contract_rules:
            hit = _best_match(self.contract_rules[contract_id], path)
            if hit is not None:
                return hit
        # 2. 공통 path 규칙
        hit = _best_match(self.common_rules, path)
        if hit is not None:
            return hit
        # 3. 필드명 휴리스틱
        leaf = path.split(".")[-1].split("[")[0].lower()
        if leaf in _HEURISTIC_EXCLUDE_EXACT:
            return Policy.EXCLUDE
        if leaf in _HEURISTIC_NORMALIZE_EXACT:
            return Policy.NORMALIZE
        if leaf.endswith(_HEURISTIC_NORMALIZE_SUFFIX):
            return Policy.NORMALIZE
        # 4. 기본 strict
        return Policy.STRICT

    def db_config_for(self, contract_id: str, table: str) -> dict[str, Any]:
        return self.db_snapshot.get(contract_id, {}).get(table, {})


def _best_match(rules: list[_Rule], path: str) -> Policy | None:
    best: _Rule | None = None
    for r in rules:
        if r.regex.fullmatch(path):
            if best is None or r.specificity > best.specificity:
                best = r
    return best.policy if best else None


def _compile_path(pattern: str) -> re.Pattern:
    """간단한 glob path를 정규식으로 컴파일한다."""
    # 토큰화: 세그먼트와 배열표기 처리
    out = []
    i = 0
    # '**' 를 자리표시자로 보호
    parts = pattern.split(".")
    regex_parts = []
    for part in parts:
        if part == "**":
            regex_parts.append(r"[^\0]*?")  # 임의 깊이 (점 포함)
            continue
        # 배열 표기: 실제 비교 경로는 인덱스를 '[]'로 일반화해 전달되므로
        # 규칙 패턴의 '[]'도 리터럴 '[]'로 매칭한다(인덱스 \d+ 변환 금지).
        seg = re.escape(part)
        seg = seg.replace(r"\*", r"[^.]*")       # 세그먼트 내 * 와일드카드
        regex_parts.append(seg)
    # '.' 로 join하되, ** 는 이미 점을 포함할 수 있으므로 점 결합을 유연하게
    joined = ""
    for idx, rp in enumerate(regex_parts):
        if idx == 0:
            joined = rp
        elif rp == r"[^\0]*?":
            joined += r"(?:\.[^\0]*?)?"  # **: 이후 점 단계 0개 이상
        elif joined.endswith(r"[^\0]*?"):
            joined += r"\.?" + rp
        else:
            joined += r"\." + rp
    return re.compile(joined)


def load_rules(path: str | Path) -> CompareRules:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return from_dict(data)


def from_dict(data: dict[str, Any]) -> CompareRules:
    common = [
        _Rule(p, Policy(pol))
        for p, pol in data.get("common", {}).get("fields", {}).items()
    ]
    contracts: dict[str, list[_Rule]] = {}
    db_snapshot: dict[str, Any] = {}
    for cid, cdef in data.get("contracts", {}).items():
        contracts[cid] = [
            _Rule(p, Policy(pol)) for p, pol in cdef.get("fields", {}).items()
        ]
        if "db_snapshot" in cdef:
            db_snapshot[cid] = cdef["db_snapshot"]
    return CompareRules(
        common_rules=common,
        contract_rules=contracts,
        db_snapshot=db_snapshot,
        meta=data.get("meta", {}),
    )
