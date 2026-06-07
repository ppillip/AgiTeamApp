"""ORM 행 -> DS-40 응답 dict 직렬화.

응답에는 normalized_text 만 노출하고 raw_text 는 노출하지 않는다 (DS-40 §21).
공개 응답 필드는 `role` 로 노출한다(QI-WG-009). DB 컬럼 role_id 는 유지.
"""
from __future__ import annotations

from typing import Any

from .models import WebguiAgentSession, WebguiMessage, WebguiRoom, WebguiRuntimeEvent


def _s(v: Any) -> Any:
    return str(v) if v is not None else None


def message_to_dict(m: WebguiMessage) -> dict[str, Any]:
    return {
        "message_id": str(m.message_id),
        "room_id": str(m.room_id),
        "correlation_id": _s(m.correlation_id),
        "role": m.role_id,
        "surface_id": m.surface_id,
        "agent_session_id": _s(m.agent_session_id),
        "direction": m.direction,
        "source": m.source,
        "message_type": m.message_type,
        "text": m.normalized_text,
        "status": m.status,
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


def room_summary_dict(r: WebguiRoom, last: WebguiMessage | None, collector_state: str = "unknown") -> dict[str, Any]:
    return {
        "room_id": str(r.room_id),
        "project_id": r.project_id,
        "role": r.role_id,
        "display_name": r.display_name,
        "agent_type": r.agent_type,
        "room_type": r.room_type,
        "surface_id": r.current_surface_id,
        "agent_session_id": _s(r.current_agent_session_id),
        "ready_state": r.ready_state,
        "collector_state": collector_state,
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
