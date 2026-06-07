"""Repository 계층 (DS-30 §7 데이터 흐름, DS-60 §9 transaction).

세션의 commit/flush 책임은 호출자(service) 가 명시적으로 진다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import WebguiAgentSession, WebguiMessage, WebguiRoom, WebguiRuntimeEvent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def to_uuid(v: str | uuid.UUID) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


# --- Room ------------------------------------------------------------------

async def get_room(db: AsyncSession, room_id: str | uuid.UUID) -> WebguiRoom | None:
    return await db.get(WebguiRoom, to_uuid(room_id))


async def get_room_by_role(db: AsyncSession, project_id: str, role_id: str) -> WebguiRoom | None:
    res = await db.execute(
        select(WebguiRoom).where(
            WebguiRoom.project_id == project_id, WebguiRoom.role_id == role_id
        )
    )
    return res.scalar_one_or_none()


async def list_rooms(db: AsyncSession, project_id: str) -> list[WebguiRoom]:
    res = await db.execute(
        select(WebguiRoom)
        .where(WebguiRoom.project_id == project_id)
        .order_by(WebguiRoom.last_message_at.desc().nullslast(), WebguiRoom.created_at.asc())
    )
    return list(res.scalars().all())


async def upsert_room(
    db: AsyncSession,
    *,
    project_id: str,
    role_id: str,
    display_name: str,
    agent_type: str | None = None,
    room_type: str = "role",
) -> WebguiRoom:
    room = await get_room_by_role(db, project_id, role_id)
    if room is None:
        room = WebguiRoom(
            project_id=project_id,
            role_id=role_id,
            display_name=display_name,
            agent_type=agent_type,
            room_type=room_type,
        )
        db.add(room)
        await db.flush()
    return room


# --- Message ---------------------------------------------------------------

async def get_message(db: AsyncSession, message_id: str | uuid.UUID) -> WebguiMessage | None:
    return await db.get(WebguiMessage, to_uuid(message_id))


async def create_message(db: AsyncSession, **fields) -> WebguiMessage:
    msg = WebguiMessage(**fields)
    db.add(msg)
    await db.flush()
    return msg


async def find_dedupe_message(
    db: AsyncSession, agent_session_id: uuid.UUID, source: str, raw_hash: str
) -> WebguiMessage | None:
    res = await db.execute(
        select(WebguiMessage).where(
            WebguiMessage.agent_session_id == agent_session_id,
            WebguiMessage.source == source,
            WebguiMessage.raw_hash == raw_hash,
        )
    )
    return res.scalars().first()


async def find_open_outbound(db: AsyncSession, room_id: uuid.UUID) -> WebguiMessage | None:
    """방의 가장 최근 'sent' outbound 중 아직 닫히지 않은 correlation 후보 (DS-60 §6.5)."""
    res = await db.execute(
        select(WebguiMessage)
        .where(
            WebguiMessage.room_id == room_id,
            WebguiMessage.direction == "outbound",
            WebguiMessage.status == "sent",
            WebguiMessage.correlation_id.isnot(None),
        )
        .order_by(WebguiMessage.occurred_at.desc())
        .limit(1)
    )
    return res.scalars().first()


async def list_messages(
    db: AsyncSession,
    room_id: uuid.UUID,
    *,
    limit: int = 50,
    direction: str = "desc",
    since: datetime | None = None,
    until: datetime | None = None,
    correlation_id: uuid.UUID | None = None,
) -> list[WebguiMessage]:
    stmt = select(WebguiMessage).where(WebguiMessage.room_id == room_id)
    if since is not None:
        stmt = stmt.where(WebguiMessage.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(WebguiMessage.occurred_at <= until)
    if correlation_id is not None:
        stmt = stmt.where(WebguiMessage.correlation_id == correlation_id)
    order = WebguiMessage.occurred_at.desc() if direction == "desc" else WebguiMessage.occurred_at.asc()
    stmt = stmt.order_by(order, WebguiMessage.message_id.asc()).limit(limit + 1)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def updates_since(
    db: AsyncSession, room_id: uuid.UUID, after: datetime | None, limit: int = 50
) -> list[WebguiMessage]:
    stmt = select(WebguiMessage).where(WebguiMessage.room_id == room_id)
    if after is not None:
        stmt = stmt.where(WebguiMessage.recorded_at > after)
    stmt = stmt.order_by(WebguiMessage.recorded_at.asc(), WebguiMessage.message_id.asc()).limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


# --- Room aggregate update --------------------------------------------------

async def touch_room_last_message(db: AsyncSession, room: WebguiRoom, msg: WebguiMessage, inbound: bool) -> None:
    room.last_message_id = msg.message_id
    room.last_message_at = msg.occurred_at
    room.updated_at = _now()
    if inbound:
        room.unread_count = (room.unread_count or 0) + 1


async def mark_read(db: AsyncSession, room: WebguiRoom, read_until: datetime | None) -> None:
    room.read_marker_at = read_until or room.last_message_at or _now()
    room.unread_count = 0
    room.updated_at = _now()


# --- Runtime event ----------------------------------------------------------

async def create_event(db: AsyncSession, **fields) -> WebguiRuntimeEvent:
    ev = WebguiRuntimeEvent(**fields)
    db.add(ev)
    await db.flush()
    return ev


async def list_events(
    db: AsyncSession, room_id: uuid.UUID, *, limit: int = 50, correlation_id: uuid.UUID | None = None
) -> list[WebguiRuntimeEvent]:
    stmt = select(WebguiRuntimeEvent).where(WebguiRuntimeEvent.room_id == room_id)
    if correlation_id is not None:
        stmt = stmt.where(WebguiRuntimeEvent.correlation_id == correlation_id)
    stmt = stmt.order_by(WebguiRuntimeEvent.occurred_at.desc()).limit(limit + 1)
    res = await db.execute(stmt)
    return list(res.scalars().all())


# --- Agent session ----------------------------------------------------------

async def get_session(db: AsyncSession, agent_session_id: str | uuid.UUID) -> WebguiAgentSession | None:
    return await db.get(WebguiAgentSession, to_uuid(agent_session_id))


async def active_session_for_room(db: AsyncSession, room_id: uuid.UUID) -> WebguiAgentSession | None:
    res = await db.execute(
        select(WebguiAgentSession)
        .where(and_(WebguiAgentSession.room_id == room_id, WebguiAgentSession.ended_at.is_(None)))
        .order_by(WebguiAgentSession.started_at.desc().nullslast())
        .limit(1)
    )
    return res.scalars().first()
