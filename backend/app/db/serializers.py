"""ORM 행 -> DS-40 응답 dict 직렬화.

응답에는 normalized_text 만 노출하고 raw_text 는 노출하지 않는다 (DS-40 §21).
공개 응답 필드는 `role` 로 노출한다(QI-WG-009). DB 컬럼 role_id 는 유지.
"""
from __future__ import annotations

from typing import Any

from .models import WebguiAgentSession, WebguiMessage, WebguiRoom, WebguiRuntimeEvent


def _s(v: Any) -> Any:
    return str(v) if v is not None else None


# 실데이터로 인정하는 origin (DS-40 §6): hook/transcript/bridge/pm_bridge 는 실제 에이전트
# 산출, webgui 는 사용자가 실제 입력한 데이터다(QI-WG-029: DS-40 §6 "...webgui...면 true").
_REAL_SOURCES = {"hook", "transcript", "bridge", "pm_bridge", "webgui"}


def provenance_dict(
    source: str | None, *, runtime_state: str = "live", transport: str | None = None
) -> dict[str, Any]:
    """DS-40 §6 Provenance: origin/runtime_state/is_real_data/is_mock/transport (QI-WG-029).

    origin: hook/transcript/bridge/pm_bridge/webgui=real, mock/None=mock, 그 외(manual/
            injected/diagnostic)=실데이터 아님이지만 목업도 아님.
    runtime_state: live | mock | disconnected. 실데이터 아니면 mock 으로 강제.
    is_mock: 목업·샘플이면 true(이때 is_real_data=false 고정, DS-40 §6).
    transport: websocket | polling | rest | internal (선택).
    """
    origin = source if source is not None else "mock"
    is_real = origin in _REAL_SOURCES
    is_mock = origin == "mock"
    rs = runtime_state if is_real else "mock"
    out: dict[str, Any] = {
        "origin": origin,
        "runtime_state": rs,          # live | mock | disconnected
        "is_real_data": is_real,
        "is_mock": is_mock,
    }
    if transport is not None:
        out["transport"] = transport
    return out


def message_to_dict(
    m: WebguiMessage,
    *,
    runtime_state: str = "live",
    transport: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """DS-40 §7.2/§8 메시지 공개 응답. project_id 는 프로젝트 격리 공개키(QI-WG-029).

    project_id 는 WebguiMessage 컬럼이 아니라 소속 room 의 값이므로 호출처가 주입한다
    (room 을 모르는 호출처는 None — 점진적 채움).
    """
    return {
        "message_id": str(m.message_id),
        "project_id": project_id,                    # DS-40 §7.2 공개계약 (QI-WG-029)
        "room_id": str(m.room_id),
        "correlation_id": _s(m.correlation_id),
        "role": m.role_id,
        "surface_id": m.surface_id,
        "agent_session_id": _s(m.agent_session_id),
        "team_session_id": m.team_session_id,        # provenance: FE 세션 구분선용 (DV-41)
        "direction": m.direction,
        "source": m.source,
        "message_type": m.message_type,
        "text": m.normalized_text,
        # 이미지 첨부 공개 메타 (DV-90, DS-40 §4.2.1). 절대경로 미포함.
        "attachments": m.attachments_json or [],
        "status": m.status,
        "provenance": provenance_dict(m.source, runtime_state=runtime_state, transport=transport),
        "occurred_at": m.occurred_at,
        "recorded_at": m.recorded_at,
        "updated_at": m.updated_at,
    }


def last_message_dict(m: WebguiMessage) -> dict[str, Any]:
    return {
        "message_id": str(m.message_id),
        "text": m.normalized_text,
        "direction": m.direction,
        "status": m.status,
        "occurred_at": m.occurred_at,
    }


def _room_runtime_state(r: WebguiRoom, connection_state: str | None) -> str:
    """방 runtime_state: cmux 연결됐으면 live, 아니면 disconnected (DV-42)."""
    if connection_state in ("connected", "live"):
        return "live"
    return "disconnected"


def room_summary_dict(
    r: WebguiRoom,
    last: WebguiMessage | None,
    collector_state: str = "unknown",
    *,
    connection_state: str | None = None,
    runtime_activity: str = "unknown",
) -> dict[str, Any]:
    runtime_state = _room_runtime_state(r, connection_state)
    last_source = last.source if last is not None else None
    return {
        "room_id": str(r.room_id),
        "project_id": r.project_id,
        "role": r.role_id,
        "display_name": r.display_name,
        "agent_type": r.agent_type,
        "room_type": r.room_type,
        "surface_id": r.current_surface_id,
        "agent_session_id": _s(r.current_agent_session_id),
        # provenance: 방의 현재 세션 식별값(식별키 아님) + 출처/실데이터/런타임 (DV-41/42)
        "team_session_id": r.team_session_id,
        "agent_id": r.agent_id,
        "ready_state": r.ready_state,
        "collector_state": collector_state,
        "runtime_state": runtime_state,
        # 동작중/조용함 (요구사항 15-1). cmux 연결(runtime_state)과 직교한 별도 축.
        "runtime_activity": runtime_activity,
        "provenance": provenance_dict(last_source, runtime_state=runtime_state),
        "last_message": last_message_dict(last) if last is not None else None,
        "last_message_at": r.last_message_at,
        "read_marker_at": r.read_marker_at,
        "unread_count": r.unread_count,
    }


def runtime_context_dict(r: WebguiRoom, collector_state: str = "unknown") -> dict[str, Any]:
    return {
        "room_id": str(r.room_id),
        "role": r.role_id,
        "display_name": r.display_name,
        "agent_type": r.agent_type,
        "surface_id": r.current_surface_id,
        "agent_session_id": _s(r.current_agent_session_id),
        "ready_state": r.ready_state,
        "collector_state": collector_state,
    }


def event_to_dict(e: WebguiRuntimeEvent) -> dict[str, Any]:
    return {
        "event_id": str(e.event_id),
        "room_id": str(e.room_id),
        "message_id": _s(e.message_id),
        "correlation_id": _s(e.correlation_id),
        "event_type": e.event_type,
        "source": e.source,
        "hook_provider": e.hook_provider,
        "hook_event_name": e.hook_event_name,
        "severity": e.severity,
        "payload": e.masked_payload_json,
        "occurred_at": e.occurred_at,
    }


def session_to_dict(s: WebguiAgentSession) -> dict[str, Any]:
    return {
        "agent_session_id": str(s.agent_session_id),
        "room_id": str(s.room_id),
        "role": s.role_id,
        "surface_id": s.surface_id,
        "agent_type": s.agent_type,
        "ready_state": s.ready_state,
        "collector_state": s.collector_state,
    }
