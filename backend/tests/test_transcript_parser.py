"""transcript_parser 단위 테스트 (DV-25, DS-60 §6.3 실측 규격).

Claude Code / Codex transcript JSONL 의 user/assistant 본문만 canonical 로 추출하고
tool_use/tool_result/reasoning/developer 성격 record 는 본문에서 제외하는지 검증한다.
"""
from __future__ import annotations

import json

from app.services.transcript_parser import (
    KIND_ASSISTANT,
    KIND_USER,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    claude_cwd_slug,
    codex_cwd_of,
    parse_claude_records,
    parse_codex_records,
    parse_records,
)


# --- Claude -----------------------------------------------------------------

def _claude_lines() -> str:
    rows = [
        {"type": "user", "uuid": "u1", "sessionId": "s1", "cwd": "/p",
         "timestamp": "2026-06-08T00:00:01Z",
         "message": {"role": "user", "content": "DS-40 작업을 검토해줘."}},
        {"type": "assistant", "uuid": "a1", "sessionId": "s1", "cwd": "/p",
         "timestamp": "2026-06-08T00:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "검토하겠습니다."},
             {"type": "tool_use", "name": "Read", "input": {}},
         ]}},
        # tool_result 만 있는 user record → 본문 비어 제외
        {"type": "user", "uuid": "u2", "sessionId": "s1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "file body"},
         ]}},
        # system/attachment 는 본문 아님
        {"type": "system", "subtype": "stop", "stopReason": "end_turn"},
        {"type": "attachment", "attachment": {"hookName": "x"}},
    ]
    return "\n".join(json.dumps(r) for r in rows)


def test_claude_extracts_user_and_assistant_only():
    recs = parse_claude_records(_claude_lines())
    assert len(recs) == 2
    u, a = recs
    assert u.kind == KIND_USER and u.record_id == "u1" and u.provider == PROVIDER_CLAUDE
    assert u.text == "DS-40 작업을 검토해줘."
    assert a.kind == KIND_ASSISTANT and a.record_id == "a1"
    # tool_use block 은 제외되고 text block 만 본문
    assert a.text == "검토하겠습니다."
    assert a.occurred_at is not None and a.session_id == "s1"


def test_claude_skips_broken_lines():
    text = '{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}\n{broken json\n'
    recs = parse_claude_records(text)
    assert len(recs) == 1 and recs[0].text == "hi"


def test_claude_cwd_slug():
    assert claude_cwd_slug("/Users/ppillip/Projects/Panthea") == "-Users-ppillip-Projects-Panthea"


# --- Codex ------------------------------------------------------------------

def _codex_lines() -> str:
    rows = [
        {"type": "session_meta", "payload": {"id": "c-sess", "cwd": "/Users/x/Projects/Panthea",
                                              "originator": "codex-tui"}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "cwd": "/Users/x/Projects/Panthea"}},
        {"type": "response_item", "timestamp": "2026-06-08T00:00:02Z",
         "payload": {"type": "message", "role": "user", "id": "r-u",
                     "content": [{"type": "input_text", "text": "착수해줘"}]}},
        {"type": "response_item", "timestamp": "2026-06-08T00:00:06Z",
         "payload": {"type": "message", "role": "assistant", "id": "r-a",
                     "content": [{"type": "output_text", "text": "착수했습니다."}]}},
        # reasoning 은 본문 제외
        {"type": "response_item",
         "payload": {"type": "reasoning", "summary": "...", "encrypted_content": "xxx"}},
        # developer role 은 본문 제외
        {"type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "system instr"}]}},
        # event_msg 표시 중복 → canonical 아님(제외)
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "착수했습니다."}},
    ]
    return "\n".join(json.dumps(r) for r in rows)


def test_codex_extracts_response_item_messages_only():
    recs = parse_codex_records(_codex_lines())
    assert len(recs) == 2
    u, a = recs
    assert u.kind == KIND_USER and u.record_id == "r-u" and u.provider == PROVIDER_CODEX
    assert u.text == "착수해줘"
    assert a.kind == KIND_ASSISTANT and a.text == "착수했습니다."
    # session_meta 의 cwd/session 이 record 에 전파
    assert a.session_id == "c-sess" and a.cwd == "/Users/x/Projects/Panthea"


def test_codex_cwd_of():
    assert codex_cwd_of(_codex_lines()) == "/Users/x/Projects/Panthea"


def test_parse_records_dispatch():
    assert parse_records("unknown_provider", _codex_lines()) == []
    assert len(parse_records(PROVIDER_CODEX, _codex_lines())) == 2
    assert len(parse_records(PROVIDER_CLAUDE, _claude_lines())) == 2
