"""hook_normalizer 단위 테스트 (DV-25, DS-60 §7.4 4 CLI 매핑)."""
from __future__ import annotations

from app.services.hook_normalizer import (
    VALID_EVENT_TYPES,
    extract_hints,
    normalize,
    normalize_event_type,
)


def test_claude_event_mapping():
    assert normalize_event_type("claude_code", "UserPromptSubmit") == "hook_user_prompt"
    assert normalize_event_type("claude_code", "PreToolUse") == "hook_pre_tool_use"
    assert normalize_event_type("claude_code", "PostToolUse") == "hook_post_tool_use"
    assert normalize_event_type("claude_code", "Stop") == "hook_stop"
    assert normalize_event_type("claude_code", "SessionStart") == "hook_session"


def test_codex_and_opencode_and_antigravity_mapping():
    assert normalize_event_type("codex", "Stop") == "hook_stop"
    assert normalize_event_type("opencode", "tool.execute.after") == "hook_post_tool_use"
    assert normalize_event_type("opencode", "message.updated") == "hook_user_prompt"
    assert normalize_event_type("antigravity", "PreInvocation") == "hook_session"
    assert normalize_event_type("antigravity", "PostToolUse") == "hook_post_tool_use"


def test_unknown_event_falls_back_by_keyword():
    # 매핑 테이블에 없는 변형 이름도 키워드로 흡수
    assert normalize_event_type("claude_code", "UserPromptSubmitV2") == "hook_user_prompt"
    assert normalize_event_type("future_cli", "onStop") == "hook_stop"
    # 완전 미상 → hook_session
    assert normalize_event_type("x", "weird") == "hook_session"


def test_all_mapped_types_valid():
    for ev in ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]:
        assert normalize_event_type("claude_code", ev) in VALID_EVENT_TYPES


def test_extract_hints():
    sid, tpath, cwd = extract_hints(
        {"session_id": "s1", "transcript_path": "/x/s1.jsonl", "cwd": "/proj"}
    )
    assert sid == "s1" and tpath == "/x/s1.jsonl" and cwd == "/proj"
    # camelCase / 대체키도 인식
    sid2, _, _ = extract_hints({"sessionId": "s2"})
    assert sid2 == "s2"
    assert extract_hints(None) == (None, None, None)


def test_normalize_full():
    norm = normalize("claude_code", "Stop", {"session_id": "s1", "transcript_path": "/p/s1.jsonl"})
    assert norm.event_type == "hook_stop"
    assert norm.source == "hook"
    assert norm.hook_provider == "claude_code"
    assert norm.severity == "info"
    assert norm.session_id == "s1" and norm.transcript_path == "/p/s1.jsonl"
