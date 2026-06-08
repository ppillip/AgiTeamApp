"""Transcript JSONL canonical parser (DV-25, DS-60 §6.3).

기존 tee raw stdout 본문 수집(폐기) 대체. 대화 본문 canonical 의 단일 출처는
provider transcript JSONL 이다. 본 모듈은 파일 IO 없는 '순수 파싱' 계층으로,
Claude Code / Codex transcript record 를 공통 ``TranscriptRecord`` 로 정규화한다.

- Claude Code: ``~/.claude/projects/<cwd-slug>/<sessionId>.jsonl``
  record type: user / assistant / attachment / system / last-prompt (DS-60 §6.3.1)
- Codex: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
  top-level type: session_meta / turn_context / event_msg / response_item (DS-60 §6.3.2)

본문 표시 대상은 user/assistant message 뿐이며 reasoning/encrypted/developer/system
성격 record 는 본문으로 승격하지 않는다(진단/비표시). tool_use/tool_result block 도
본문 텍스트로 보지 않는다.

파일 탐색/offset tail/DB 저장은 transcript_collector 가 담당한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROVIDER_CLAUDE = "claude_code"
PROVIDER_CODEX = "codex"

KIND_USER = "user_message"
KIND_ASSISTANT = "assistant_message"


@dataclass
class TranscriptRecord:
    """정규화된 transcript 본문 record (canonical inbound/outbound 후보)."""

    provider: str            # claude_code | codex
    record_id: str | None    # Claude uuid / Codex record id (없으면 None → raw_hash 로 dedupe)
    kind: str                # user_message | assistant_message
    text: str                # 표시용 정규화 텍스트
    occurred_at: datetime | None
    session_id: str | None = None
    cwd: str | None = None


# --- 공통 유틸 --------------------------------------------------------------

def _parse_ts(value) -> datetime | None:
    """ISO-8601(Z 포함) 또는 epoch 초/밀리초 → aware datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # 13자리(ms) 휴리스틱
        ts = value / 1000.0 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _coerce_text(content) -> str:
    """message.content (str | list[block] | None) 에서 표시용 본문 텍스트만 추출.

    - str → 그대로
    - list → ``type in (text, output_text, input_text)`` block 의 text 만 join.
      tool_use / tool_result / image / reasoning / thinking block 은 본문 제외.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("text", "output_text", "input_text"):
                t = block.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t)
            # tool_use / tool_result / image / reasoning / thinking 등은 본문 제외
        return "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return ""


def _loads_lines(text: str):
    """JSONL 본문 → (dict) generator. 깨진 라인(부분 flush 등)은 건너뛴다."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            yield obj


# --- Claude Code ------------------------------------------------------------

def parse_claude_records(text: str) -> list[TranscriptRecord]:
    """Claude Code transcript JSONL 본문 → TranscriptRecord 목록.

    type=user/assistant 의 message.content 텍스트만 본문으로 승격한다.
    tool_result 만 있는 user record(=도구 결과)는 본문이 비므로 자연히 제외된다.
    """
    out: list[TranscriptRecord] = []
    for obj in _loads_lines(text):
        rtype = obj.get("type")
        if rtype not in ("user", "assistant"):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        body = _coerce_text(message.get("content"))
        if not body:
            continue
        kind = KIND_USER if rtype == "user" else KIND_ASSISTANT
        out.append(
            TranscriptRecord(
                provider=PROVIDER_CLAUDE,
                record_id=obj.get("uuid"),
                kind=kind,
                text=body,
                occurred_at=_parse_ts(obj.get("timestamp")),
                session_id=obj.get("sessionId"),
                cwd=obj.get("cwd"),
            )
        )
    return out


def claude_cwd_slug(project_root: str | Path) -> str:
    """절대경로 → Claude projects dir slug. ``/`` 를 ``-`` 로 치환(실측, DS-60 §6.3.1).

    예: /Users/ppillip/Projects/Panthea → -Users-ppillip-Projects-Panthea
    """
    abs_path = str(Path(project_root).resolve())
    return abs_path.replace("/", "-")


# --- Codex ------------------------------------------------------------------

def parse_codex_records(text: str) -> list[TranscriptRecord]:
    """Codex rollout JSONL 본문 → TranscriptRecord 목록.

    canonical 은 ``response_item`` + payload.type=message + role in(user,assistant).
    developer/system role 과 reasoning/encrypted_content 는 본문 제외(DS-60 §6.3.2).
    payload.content 는 list 구조이므로 text item 만 추출한다.
    event_msg(user_message/agent_message) 는 표시 중복이므로 본문 canonical 로 쓰지 않는다.
    """
    out: list[TranscriptRecord] = []
    cwd: str | None = None
    session_id: str | None = None
    for obj in _loads_lines(text):
        ttype = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if ttype == "session_meta":
            cwd = payload.get("cwd") or cwd
            session_id = payload.get("id") or session_id
            continue
        if ttype == "turn_context":
            cwd = payload.get("cwd") or cwd
            continue
        if ttype != "response_item":
            continue
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            # developer / system / tool 성격 role 은 본문 제외
            continue
        body = _coerce_text(payload.get("content"))
        if not body:
            continue
        kind = KIND_USER if role == "user" else KIND_ASSISTANT
        out.append(
            TranscriptRecord(
                provider=PROVIDER_CODEX,
                record_id=payload.get("id"),
                kind=kind,
                text=body,
                occurred_at=_parse_ts(obj.get("timestamp") or payload.get("timestamp")),
                session_id=session_id,
                cwd=cwd,
            )
        )
    return out


def codex_cwd_of(text: str) -> str | None:
    """rollout JSONL 의 session_meta.payload.cwd 를 반환(프로젝트 매핑용)."""
    for obj in _loads_lines(text):
        if obj.get("type") == "session_meta":
            payload = obj.get("payload")
            if isinstance(payload, dict):
                return payload.get("cwd")
    return None


# --- provider dispatch ------------------------------------------------------

def parse_records(provider: str, text: str) -> list[TranscriptRecord]:
    if provider == PROVIDER_CLAUDE:
        return parse_claude_records(text)
    if provider == PROVIDER_CODEX:
        return parse_codex_records(text)
    return []
