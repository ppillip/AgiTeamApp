"""Collector 내부 수집 스키마 (DS-40 §15.1, §15.4 / DS-60 §6.7, DV-25 정정).

수집 방향 확정: 대화 본문 canonical = bridge/hook/transcript.
- transcript JSONL parser 가 inbound 본문(source=transcript)을 저장한다.
- raw role log/read-screen 은 본문이 아니라 runtime_event 로 분리한다.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CollectMessageRequest(BaseModel):
    agent_session_id: str | None = None       # transcript 수집은 session 미상일 수 있음(hook 보강 전)
    role_id: str
    surface_id: str | None = None
    source: str               # bridge | hook | transcript
    message_type: str         # user_message | assistant_message | status | error | unmatched
    # transcript 추적 필드 (DS-30 §4.3 / DS-60 §6.7)
    provider: str | None = None               # claude_code | codex | opencode | antigravity
    transcript_path: str | None = None        # 마스킹된 transcript 상대 식별자
    transcript_offset: str | None = None       # JSONL byte offset / line number 후보
    transcript_record_id: str | None = None    # Claude uuid / Codex record id
    raw_text: str | None = None
    normalized_text: str
    raw_hash: str | None = None
    correlation_id: str | None = None
    occurred_at: datetime


class CollectEventRequest(BaseModel):
    agent_session_id: str | None = None
    message_id: str | None = None
    correlation_id: str | None = None
    event_type: str | None = None             # 미지정 시 hook_normalizer 가 provider+event_name 으로 정규화
    source: str               # cmux_adapter | conversation_collector | transcript_parser | raw_log_collector | hook | read_screen | backend | artifact_service | postgres_notify
    hook_provider: str | None = None
    hook_event_name: str | None = None
    severity: str = "info"
    payload: dict | None = None
    occurred_at: datetime


class HookCollectRequest(BaseModel):
    """roomless hook 수집 (DS-40/60, 닭달걀 해소).

    hook 발신 시점에는 room_id 를 모른다(방은 부팅 후 생성). 그래서 URL 에 room_id 가 없고
    canonical 안정키 (project_id, role) 로 방을 upsert 한 뒤 그 방에 hook 처리 +
    transcript 즉시수집한다 (QI-WG-022 정합). 1 역할 1 방, 재부팅에도 동일 방 유지.
    team_session_id / agent_id 는 방 식별키가 아니라 현재 세션·provenance 검증값이다.

    필드명은 HOOK 계약 round2 §2 (log_stop.sh POST body) 와 1:1 정합한다:
    project_id, role(필수), team_session_id, agent_id, hook_provider, occurred_at,
    transcript_path, hook_stdin(보강). 런처가 보내는 top-level 필드를 그대로 수용한다.
    round-1 별칭(cli, payload)은 하위호환으로 함께 수용한다.
    """
    model_config = ConfigDict(extra="ignore")

    project_id: str
    role: str                                  # 방 canonical 안정키(project_id + role)
    team_session_id: str | None = None         # provenance: 팀 부팅 1회 실행 식별자(TEAM_SESSION_ID)
    agent_id: str | None = None                # provenance: 현재 실행 agent(AGENT_ID). 방 식별키 아님
    hook_provider: str | None = None           # 계약 §2: claude_code | codex (1차)
    cli: str | None = None                     # round-1 별칭: claude | claude_code | codex
    # 계약 §2 body 는 hook_event_name 을 싣지 않는다(log_stop.sh = Stop hook). 미지정 시 Stop.
    hook_event_name: str = "Stop"              # SessionStart | UserPromptSubmit | Stop | ...
    session_id: str | None = None
    transcript_path: str | None = None         # transcript JSONL 경로 또는 record ref
    cwd: str | None = None
    display_name: str | None = None            # 방 표시명(미지정 시 role)
    severity: str = "info"
    hook_stdin: dict | None = None             # 계약 §2: 원본 hook stdin 보강(있으면 hint 병합)
    payload: dict | None = None                # round-1 별칭
    occurred_at: datetime | None = None
