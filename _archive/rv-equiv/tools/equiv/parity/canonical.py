"""Canonical JSON 변환.

TS-11 §3.1 / TS-10 §7: JSON·JSONB object key order는 normalize 정책상 canonical
key sort 후 비교한다. 바이트 비교는 금지(직렬화기가 달라 100% 실패)이며, 반드시
파싱 후 의미 비교(canonical JSON)를 한다.

규칙
- dict: 키를 정렬해 재귀 canonical화
- list: 순서 보존(정렬 보장 배열은 비교 단계에서 strict, 미보장 배열은 rules에서
  sort 지정). canonical화 자체는 순서를 바꾸지 않는다.
- scalar: 그대로
"""

from __future__ import annotations

import json
from typing import Any


def canonicalize(value: Any) -> Any:
    """object key order를 제거한 canonical 형태로 재귀 변환한다.

    list 순서는 보존한다. 순서 비교 정책은 compare 단계에서 결정한다.
    """
    if isinstance(value, dict):
        return {k: canonicalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    return value


def canonical_dumps(value: Any) -> str:
    """canonical JSON 문자열. 디버깅·증거 저장용 (sort_keys + 안정 구분자)."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def maybe_parse_json(value: Any) -> Any:
    """JSONB 컬럼이 문자열로 덤프된 경우 파싱을 시도한다.

    DB 덤프 시 jsonb가 텍스트로 직렬화되면 Python/Rust 직렬화기 차이로 키 순서가
    달라질 수 있다. 파싱 가능하면 구조로 되돌려 canonical 비교 대상으로 만든다.
    파싱 불가하면 원본 그대로 반환한다.
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or s[0] not in "{[":
        return value
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return value
