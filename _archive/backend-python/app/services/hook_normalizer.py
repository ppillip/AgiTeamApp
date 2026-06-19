"""HookEventNormalizer (DV-25, DS-60 §7).

provider 별 hook payload 를 WebGUI 공통 ``runtime_event`` 분류로 정규화한다.
assistant/user 본문은 hook 단독이 아니라 transcript JSONL parser 가 생성하는
``webgui_message`` 를 canonical 로 삼는다. hook 은 session/transcript 위치와
Stop boundary, tool 실행 이력, correlation hint 를 제공하는 역할을 우선한다.

Claude Code / Codex / opencode / antigravity 4개 CLI 를 모두 지원한다(DS-60 §7.4).
"""
from __future__ import annotations

from dataclasses import dataclass

# (provider, 원본 event_name) → 공통 event_type (DS-60 §7.4)
_EVENT_TYPE_MAP: dict[tuple[str, str], str] = {
    # Claude Code
    ("claude_code", "SessionStart"): "hook_session",
    ("claude_code", "UserPromptSubmit"): "hook_user_prompt",
    ("claude_code", "PreToolUse"): "hook_pre_tool_use",
    ("claude_code", "PostToolUse"): "hook_post_tool_use",
    ("claude_code", "Stop"): "hook_stop",
    # Codex
    ("codex", "SessionStart"): "hook_session",
    ("codex", "UserPromptSubmit"): "hook_user_prompt",
    ("codex", "PreToolUse"): "hook_pre_tool_use",
    ("codex", "PostToolUse"): "hook_post_tool_use",
    ("codex", "Stop"): "hook_stop",
    # opencode (plugin event)
    ("opencode", "session.status"): "hook_session",
    ("opencode", "message.updated"): "hook_user_prompt",
    ("opencode", "tool.execute.before"): "hook_pre_tool_use",
    ("opencode", "tool.execute.after"): "hook_post_tool_use",
    # antigravity
    ("antigravity", "PreInvocation"): "hook_session",
    ("antigravity", "PostInvocation"): "hook_session",
    ("antigravity", "PreToolUse"): "hook_pre_tool_use",
    ("antigravity", "PostToolUse"): "hook_post_tool_use",
    ("antigravity", "Stop"): "hook_stop",
}

# 이름 기반 보조 분류(매핑에 없는 변형 event_name 흡수)
_NAME_KEYWORD_MAP: list[tuple[str, str]] = [
    ("userpromptsubmit", "hook_user_prompt"),
    ("pretool", "hook_pre_tool_use"),
    ("posttool", "hook_post_tool_use"),
    ("tool.execute.before", "hook_pre_tool_use"),
    ("tool.execute.after", "hook_post_tool_use"),
    ("stop", "hook_stop"),
    ("session", "hook_session"),
    ("invocation", "hook_session"),
]

VALID_EVENT_TYPES = {
    "hook_session",
    "hook_user_prompt",
    "hook_pre_tool_use",
    "hook_post_tool_use",
    "hook_stop",
}

# transcript correlation hint 로 쓰는 payload 키 후보
_SESSION_ID_KEYS = ("session_id", "sessionId", "id")
_TRANSCRIPT_PATH_KEYS = ("transcript_path", "transcriptPath", "rollout_path", "transcript")
_CWD_KEYS = ("cwd", "workspace", "project_root", "working_directory")
# 방 라우팅 1차 키(유저 확정 2026-06-08): 1에이전트=1방. Atlas 가 hook payload 에 주입.
_AGENT_ID_KEYS = ("agent_id", "agentId", "AGENT_ID")


@dataclass
class NormalizedHookEvent:
    event_type: str
    source: str
    hook_provider: str
    hook_event_name: str
    severity: str
    # transcript parser boundary/correlation 보강용 hint
    session_id: str | None = None
    transcript_path: str | None = None
    cwd: str | None = None
    agent_id: str | None = None      # 방 라우팅 1차 키 (1에이전트=1방)


def normalize_event_type(provider: str, event_name: str) -> str:
    """provider+event_name → 공통 event_type. 미상은 이름 키워드로 보조 분류, 최후엔 hook_session."""
    key = ((provider or "").strip().lower(), (event_name or "").strip())
    mapped = _EVENT_TYPE_MAP.get(key)
    if mapped:
        return mapped
    lname = (event_name or "").strip().lower()
    for needle, etype in _NAME_KEYWORD_MAP:
        if needle in lname:
            return etype
    return "hook_session"


def _first(payload: dict | None, keys: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def extract_hints(payload: dict | None) -> tuple[str | None, str | None, str | None]:
    """payload 에서 (session_id, transcript_path, cwd) correlation hint 추출."""
    return (
        _first(payload, _SESSION_ID_KEYS),
        _first(payload, _TRANSCRIPT_PATH_KEYS),
        _first(payload, _CWD_KEYS),
    )


def normalize(
    provider: str,
    event_name: str,
    payload: dict | None = None,
    severity: str | None = None,
) -> NormalizedHookEvent:
    """provider hook 이벤트를 공통 runtime_event 분류로 정규화한다.

    severity 기본값은 info. hook_stop 은 correlation_closed boundary hint 로 사용된다.
    """
    event_type = normalize_event_type(provider, event_name)
    session_id, transcript_path, cwd = extract_hints(payload)
    agent_id = _first(payload, _AGENT_ID_KEYS)
    return NormalizedHookEvent(
        event_type=event_type,
        source="hook",
        hook_provider=(provider or "").strip().lower() or "unknown",
        hook_event_name=(event_name or "").strip(),
        severity=(severity or "info"),
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=cwd,
        agent_id=agent_id,
    )
