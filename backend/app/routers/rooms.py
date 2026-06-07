"""팀원별 채팅 저장·조회 API (DV-20.2): WG-CHAT-01/02/03/04.

[관찰 뷰] 이 라우터는 PM↔팀원 대화를 role/surface 별로 분리 '조회'만 제공한다.
팀원 surface 로 직접 송신하는 경로는 없다 (송신은 WG-MSG-02 PM 경유 단일 경로).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import errors
from ..config import get_settings
from ..db import repositories as repo
from ..db.serializers import event_to_dict, message_to_dict, room_summary_dict
from ..deps import get_db, require_auth
from ..schemas.common import ok
from ..schemas.message import ReadRequest
from ..services.cmux_discovery import registry

router = APIRouter(prefix="/api/webgui/rooms", tags=["rooms"])


async def _last_message(db: AsyncSession, room):
    if room.last_message_id is None:
        return None
    return await repo.get_message(db, room.last_message_id)


def _with_connection(d: dict, project_id: str, role_id: str) -> dict:
    info = registry.resolve(project_id, role_id)
    d["connection_state"] = info.connection_state if info else "disconnected"
    return d


@router.get("", dependencies=[Depends(require_auth)])
async def list_rooms(
    project_id: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    role_id: str | None = Query(default=None),
    with_last_message: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    pid = project_id or settings.project_id
    rooms = await repo.list_rooms(db, pid)
    if role_id:
        rooms = [r for r in rooms if r.role_id == role_id]
    out = []
    for r in rooms:
        last = await _last_message(db, r) if with_last_message else None
        session = await repo.active_session_for_room(db, r.room_id)
        cs = session.collector_state if session else "unknown"
        out.append(_with_connection(room_summary_dict(r, last, cs), pid, r.role_id))
    return ok({"project_id": pid, "rooms": out})


@router.get("/{room_id}/messages", dependencies=[Depends(require_auth)])
async def list_messages(
    room_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    direction: str = Query(default="desc"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    include_events: bool = Query(default=False),
    correlation_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    if direction not in ("asc", "desc"):
        raise errors.WebguiError("invalid_pagination", 422, "direction must be asc|desc")
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()
    corr = repo.to_uuid(correlation_id) if correlation_id else None
    rows = await repo.list_messages(
        db, room.room_id, limit=limit, direction=direction, since=since, until=until, correlation_id=corr
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    last = await _last_message(db, room)
    session = await repo.active_session_for_room(db, room.room_id)
    cs = session.collector_state if session else "unknown"
    next_cursor = (
        f"{rows[-1].occurred_at.isoformat()}|message:{rows[-1].message_id}" if rows and has_more else None
    )
    return ok(
        {
            "room": room_summary_dict(room, last, cs),
            "messages": [message_to_dict(m) for m in rows],
            "page": {"limit": limit, "next_cursor": next_cursor, "has_more": has_more},
        }
    )


@router.post("/{room_id}/read", dependencies=[Depends(require_auth)])
async def mark_read(room_id: str, body: ReadRequest, db: AsyncSession = Depends(get_db)):
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()
    if body.last_read_message_id:
        m = await repo.get_message(db, body.last_read_message_id)
        if m is None or str(m.room_id) != str(room.room_id):
            raise errors.WebguiError("message_room_mismatch", 409, "Message does not belong to room.")
    await repo.mark_read(db, room, body.read_until)
    await db.commit()
    return ok(
        {
            "room_id": str(room.room_id),
            "read_marker_at": room.read_marker_at,
            "unread_count": room.unread_count,
            "updated_at": room.updated_at,
        }
    )


@router.get("/{room_id}/events", dependencies=[Depends(require_auth)])
async def list_events(
    room_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    correlation_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()
    corr = repo.to_uuid(correlation_id) if correlation_id else None
    rows = await repo.list_events(db, room.room_id, limit=limit, correlation_id=corr)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ok(
        {
            "events": [event_to_dict(e) for e in rows],
            "page": {"limit": limit, "next_cursor": None, "has_more": has_more},
        }
    )
