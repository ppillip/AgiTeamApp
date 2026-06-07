"""Secret 마스킹 유틸 (DS-30 §4.4 / DS-60 §13).

저장 전 1차 마스킹을 적용한다. API key/token/Authorization/홈 절대경로 원문을
payload/로그/응답에 남기지 않는다.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "***MASKED***"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\bAuthorization\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),                      # OpenAI 계열 키
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),               # Anthropic 키
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),              # GitHub 토큰
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),                      # 긴 토큰성 문자열
]

# 사용자 홈 절대경로 -> 상대화
_HOME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/Users/[^/\s]+"),
    re.compile(r"/home/[^/\s]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
]


def mask_text(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for p in _HOME_PATTERNS:
        out = p.sub("~", out)
    for p in _PATTERNS:
        out = p.sub(MASK, out)
    return out


def mask_payload(payload: Any) -> Any:
    """dict/list/str 재귀 마스킹. 키 이름이 secret 계열이면 값을 통째로 마스킹."""
    sensitive_key = re.compile(r"(?i)(authorization|api[_-]?key|secret|token|password|passwd|cookie)")
    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for k, v in payload.items():
            if isinstance(k, str) and sensitive_key.search(k):
                result[k] = MASK
            else:
                result[k] = mask_payload(v)
        return result
    if isinstance(payload, list):
        return [mask_payload(v) for v in payload]
    if isinstance(payload, str):
        return mask_text(payload)
    return payload
