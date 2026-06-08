"""ORM 행 -> DS-40 응답 dict 직렬화.

응답에는 normalized_text 만 노출하고 raw_text 는 노출하지 않는다 (DS-40 §21).
공개 응답 필드는 `role` 로 노출한다(QI-WG-009). DB 컬럼 role_id 는 유지.
"""
from __future__ import annotations

from typing import Any

from .models import WebguiAgentSession, WebguiMessage, WebguiRoom, WebguiRuntimeEvent


def _s(v: Any) -> Any:
    return str(v) if v is not None else None


# 실데이터로 인정하는 source (DS-40 provenance): hook/transcript/bridge 는 실제 에이전트 산출.
_REAL_SOURCES = {"hook", "transcript", "bridge", "pm_bridge"}
# 수동(UI 사용자 입력) source
_MANUAL_SOURCES = {"webgui"}


def provenance_dict(source: str | None, *, runtime_state: str = "live") -> dict[str, Any]:
    """DS-40 Provenance: 메시지/방의 출처·실데이터 여부·런타임 상태.

    source: hook/transcript/bridge=real, webgui=manual(수동), 그 외=mock.
    runtime_state: live | disconnected | mock. 실데이터 아니면 mock 으로 강제.
    """
    if source in _REAL_SOURCES:
        kind = "real"
    elif source in _MANUAL_SOURCES:
        kind = "manual"
    else:
        kind = "mock"
    is_real = kind == "real"
    rs = runtime_state if is_real else ("manual" if kind == "manual" else "mock")
    return {
        "source": source,
        "kind": kind,                 # real | manual | mock
        "is_real_data": is_real,
        "runtime_state": rs,          # live | disconnected | mock | manual
    }


def message_to_dict(m: WebguiMessage, *, runtime_state: str = "live") -> dict[str, Any]:
    return {
        "message_id": str(m.message_id),
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
        "status": m.status,
        "provenance": provenance_dict(m.source, runtime_state=runtime_state),
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
