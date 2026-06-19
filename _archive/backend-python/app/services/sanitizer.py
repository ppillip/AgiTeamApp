"""대화 본문 sanitize — tool-call 누출 차단 (긴급 결함 2026-06-10).

증상: PM/에이전트 출력이 도구 호출(tool_use)을 실제 전송하지 못하고 텍스트로 흘려,
`<invoke name=...>`, `<parameter ...>`, `</invoke>`, 단독 'course' 직후 태그 조각 등
tool-call 마크업이 대화 본문(normalized_text)으로 수집·표시된다.

근본 원인(모델이 명령을 텍스트로 흘림)은 코드로 막을 수 없다. 본 모듈의 목적은
**오염 텍스트가 DB normalized_text 로 저장·표시되는 것을 차단**하는 것이다(저장 직전 1차 방어).

설계 원칙:
- tool-call 특유 마크업만 타게팅한다(invoke/parameter/function_calls/function_results/
  antml: 네임스페이스). 일반 산문에는 등장하지 않는 형태이므로 정상 메시지 손실 위험이 낮다.
- 완전한 블록(`<invoke ...>...</invoke>`)은 통째로 제거하고, 닫는 태그 없이 누출된 개별
  태그 잔편은 태그만 제거한다.
- 'course' 단독 라인은 tool 마크업 직전에 있을 때만 제거한다(일반 단어 'course' 보호).
"""
from __future__ import annotations

import re

# 1) 완전한 누출 블록(내용 포함 통째 제거). function_calls 가 invoke 를 감싸므로 먼저 제거.
_FUNCTION_BLOCK_RE = re.compile(
    r"<(?:antml:)?function_calls\b.*?</(?:antml:)?function_calls>", re.DOTALL | re.IGNORECASE
)
_INVOKE_BLOCK_RE = re.compile(
    r"<(?:antml:)?invoke\b.*?</(?:antml:)?invoke>", re.DOTALL | re.IGNORECASE
)

# 2) 단독 라인 'course' 가 바로 다음 줄 tool 마크업('<...') 직전에 있을 때만 제거.
_COURSE_BEFORE_TAG_RE = re.compile(r"(?im)^[ \t]*course[ \t]*\r?\n(?=[ \t]*<)")

# 3) 닫는 태그 없이 누출된 개별 tool 태그 잔편(태그만 제거, 인접 본문은 보존).
_TOOL_TAG_RE = re.compile(
    r"</?(?:antml:)?(?:invoke|parameter|function_calls|function_results|function)\b[^>]*/?>",
    re.IGNORECASE,
)

# 4) 정리: 공백+개행, 3줄 이상 연속 개행 축약.
_TRAIL_WS_RE = re.compile(r"[ \t]+(\r?\n)")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def sanitize_tool_leak(text: str | None) -> str | None:
    """tool-call 누출 마크업을 제거한 본문을 돌려준다. None/빈값은 그대로 통과.

    raw_text/raw_hash 등 원본 기반 값(중복 판정)에는 적용하지 않는다 — 표시·저장되는
    normalized_text 에만 적용해 dedup 거동을 바꾸지 않는다.
    """
    if not text:
        return text
    # 'course' 단독 라인 제거를 블록 제거보다 먼저 한다 — 블록을 먼저 지우면 course 직후의
    # '<' 가 사라져 course 가 고아 잔편으로 남는다.
    s = _COURSE_BEFORE_TAG_RE.sub("", text)
    s = _FUNCTION_BLOCK_RE.sub("", s)
    s = _INVOKE_BLOCK_RE.sub("", s)
    s = _TOOL_TAG_RE.sub("", s)
    s = _TRAIL_WS_RE.sub(r"\1", s)
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


def has_tool_leak(text: str | None) -> bool:
    """본문에 tool-call 누출 마크업이 포함되어 있는지(진단/로그용)."""
    if not text:
        return False
    return bool(
        _FUNCTION_BLOCK_RE.search(text)
        or _INVOKE_BLOCK_RE.search(text)
        or _TOOL_TAG_RE.search(text)
    )
