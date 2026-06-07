"""Collector 수집 서비스 (DV-20.1 응답 수신).

설계: DS-20 §11.4, DS-40 §15, DS-60 §6.
- role_log collector 가 수집한 팀원/PM 응답 본문(log_line)을 inbound message 로 저장.
- hook/cmux/read-screen 이벤트를 runtime_event 로 저장.
- 수집 대상은 PM·팀원 모든 방 (팀원별 채팅방은 이렇게 채워지는 관찰 뷰).
- correlation 매칭: open outbound 가 있으면 연결, 없으면 unmatched 보존.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from .. import errors
from ..db import repositories as repo
from ..db.serializers import event_to_dict, message_to_dict
from .events import hub
from .masking import mask_payload, mask_text


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def collect_message(db: AsyncSession, room_id: str, body) -> dict:
    """WG-CHAT-05: collector 가 수집한 inbound 메시지 저장."""
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()

    session = await repo.get_session(db, body.agent_session_id)
    if session is None:
        raise errors.WebguiError("agent_session_not_found", 404, "Agent session not found.")
    if str(session.room_id) != str(room.room_id):
        raise errors.WebguiError("session_room_mismatch", 409, "Session does not belong to room.")
    if body.role_id != room.role_id:
        raise errors.room_role_mismatch()

    # 중복 방지 (DS-30 §4.3 dedupe)
    if body.raw_hash:
        existing = await repo.find_dedupe_message(
            db, session.agent_session_id, body.source, body.raw_hash
        )
        if existing is not None:
            return {"message": message_to_dict(existing), "deduplicated": True}

    # correlation 매칭
    correlation_id = body.correlation_id
    status = "received"
    if correlation_id is None:
        open_ob = await repo.find_open_outbound(db, room.room_id)
        if open_ob is not None:
            correlation_id = open_ob.correlation_id
        else:
            status = "unmatched"

    msg = await repo.create_message(
        db,
        room_id=room.room_id,
        agent_session_id=session.agent_session_id,
        correlation_id=correlation_id,
        role_id=room.role_id,
        surface_id=body.surface_id,
        direction="inbound",
        source=body.source,
        message_type=body.message_type if status != "unmatched" else "unmatched",
        raw_text=mask_text(body.raw_text),
        normalized_text=body.normalized_text,
        raw_hash=body.raw_hash,
        status=status,
        occurred_at=body.occurred_at,
    )
    await repo.touch_room_last_message(db, room, msg, inbound=True)
    await db.commit()

    payload = message_to_dict(msg)
    await hub.publish(
        str(room.room_id),
        {
            "type": "message_update",
            "cursor": f"{msg.recorded_at.isoformat()}|message:{msg.message_id}",
            "data": {
                "update_id": f"message:{msg.message_id}",
                "room_id": str(room.room_id),
                "correlation_id": str(correlation_id) if correlation_id else None,
                "update_type": "message_received",
                "message": payload,
                "event": None,
                "occurred_at": msg.occurred_at.isoformat(),
            },
        },
    )
    return {"message": payload, "deduplicated": False}


async def collect_event(db: AsyncSession, room_id: str, body) -> dict:
    """WG-CHAT-06: hook/collector/cmux/read-screen runtime_event 저장."""
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()

    masked = mask_payload(body.payload) if body.payload is not None else None
    ev = await repo.create_event(
        db,
        room_id=room.room_id,
        agent_session_id=body.agent_session_id,
        message_id=body.message_id,
        correlation_id=body.correlation_id,
        event_type=body.event_type,
        source=body.source,
        hook_provider=body.hook_provider,
        hook_event_name=body.hook_event_name,
        severity=body.severity,
        payload_json=masked,
        masked_payload_json=masked,
        occurred_at=body.occurred_at,
    )
    await db.commit()

    # hook_stop -> correlation_closed update (DS-40 §15.4)
    update_type = "correlation_closed" if body.event_type == "hook_stop" else "runtime_error" if body.severity == "error" else None
    if update_type:
        await hub.publish(
            str(room.room_id),
            {
                "type": "message_update",
                "cursor": f"{ev.recorded_at.isoformat()}|event:{ev.event_id}",
                "data": {
                    "update_id": f"event:{ev.event_id}",
                    "room_id": str(room.room_id),
                    "correlation_id": str(body.correlation_id) if body.correlation_id else None,
                    "update_type": update_type,
                    "message": None,
                    "event": {"event_type": ev.event_type, "source": ev.source, "severity": ev.severity},
                    "occurred_at": ev.occurred_at.isoformat(),
                },
            },
        )
    return {"event": event_to_dict(ev)}
