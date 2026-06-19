"""Repository 계층 (DS-30 §7 데이터 흐름, DS-60 §9 transaction).

세션의 commit/flush 책임은 호출자(service) 가 명시적으로 진다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
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
        select(WebguiRoom)
        .where(WebguiRoom.project_id == project_id, WebguiRoom.role_id == role_id)
        .order_by(WebguiRoom.created_at.asc())
    )
    return res.scalars().first()


async def get_room_by_agent(
    db: AsyncSession, project_id: str, team_session_id: str | None, agent_id: str
) -> WebguiRoom | None:
    res = await db.execute(
        select(WebguiRoom).where(
            WebguiRoom.project_id == project_id,
            WebguiRoom.team_session_id == team_session_id,
            WebguiRoom.agent_id == agent_id,
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


async def distinct_projects_with_rooms(db: AsyncSession) -> list[dict]:
    """실제 방을 보유한 project_id 집계 (QI-WG-021): UI 프로젝트 목록 도달 보장.

    cmux 디스커버리에 안 떠도 DB 에 방/메시지가 있는 프로젝트(예: hook E2E 로 생성된
    HookTest)를 선택 가능하게 노출한다. project_id 별 room_count·roles·최신 메시지 시각 반환.
    """
    res = await db.execute(
        select(
            WebguiRoom.project_id,
            func.count().label("room_count"),
            func.max(WebguiRoom.last_message_at).label("last_message_at"),
        )
        .group_by(WebguiRoom.project_id)
        .order_by(func.max(WebguiRoom.last_message_at).desc().nullslast(), WebguiRoom.project_id)
    )
    rows = res.all()
    roles_res = await db.execute(
        select(WebguiRoom.project_id, WebguiRoom.role_id).order_by(WebguiRoom.project_id)
    )
    roles_by_project: dict[str, list[str]] = {}
    for pid, role in roles_res.all():
        roles_by_project.setdefault(pid, []).append(role)
    out: list[dict] = []
    for pid, room_count, last_message_at in rows:
        out.append(
            {
                "project_id": pid,
                "room_count": int(room_count),
                "roles": roles_by_project.get(pid, []),
                "last_message_at": last_message_at.isoformat() if last_message_at else None,
            }
        )
    return out


async def upsert_room(
    db: AsyncSession,
    *,
    project_id: str,
    role_id: str,
    display_name: str,
    agent_type: str | None = None,
    room_type: str = "role",
    team_session_id: str | None = None,
    agent_id: str | None = None,
) -> WebguiRoom:
    """방 upsert — canonical 안정키 = (project_id, role_id) (QI-WG-022).

    1 프로젝트 1 역할 1 방. team_session_id / agent_id 는 방 식별키가 아니라 현재 실행
    세션·provenance 검증값으로, 같은 (project_id, role_id) 방에 도착할 때마다 최신값으로
    갱신한다. 재부팅(team_session_id 변경)·agent 변화로 방을 새로 만들지 않고 이력을 유지한다.
    """
    room = None
    if agent_id is not None:
        room = await get_room_by_agent(db, project_id, team_session_id, agent_id)
    if room is None:
        room = await get_room_by_role(db, project_id, role_id)
    if room is None:
        room = WebguiRoom(
            project_id=project_id,
            role_id=role_id,
            display_name=display_name,
            agent_type=agent_type,
            room_type=room_type,
            team_session_id=team_session_id,
            agent_id=agent_id,
        )
        db.add(room)
    else:
        # 현재 세션 provenance 갱신 (제공된 값만)
        if team_session_id is not None:
            room.team_session_id = team_session_id
        if agent_id is not None:
            room.agent_id = agent_id
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


async def find_message_by_record(
    db: AsyncSession, provider: str, transcript_record_id: str
) -> WebguiMessage | None:
    """transcript record 중복 탐지 (provider, transcript_record_id) — DS-30 §5, DV-25."""
    res = await db.execute(
        select(WebguiMessage).where(
            WebguiMessage.provider == provider,
            WebguiMessage.transcript_record_id == transcript_record_id,
        )
    )
    return res.scalars().first()


async def find_message_by_hash(
    db: AsyncSession, room_id: uuid.UUID, source: str, raw_hash: str
) -> WebguiMessage | None:
    """room+source+raw_hash 중복 탐지 (record_id 부재 transcript/ bridge dedupe 보조)."""
    res = await db.execute(
        select(WebguiMessage).where(
            WebguiMessage.room_id == room_id,
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
    limit: int = 20,
    direction: str = "desc",
    since: datetime | None = None,
    until: datetime | None = None,
    correlation_id: uuid.UUID | None = None,
    before: tuple[datetime, uuid.UUID] | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
) -> list[WebguiMessage]:
    """방 메시지 페이지 조회 (DV-41 페이지네이션).

    direction=desc(기본): 최신부터. before 커서(=현재 표시된 가장 오래된 메시지)보다 더
    오래된 메시지를 keyset 으로 가져온다(위로 스크롤 = 과거 추가 로드).
    limit+1 을 읽어 호출자가 has_more 를 판정한다.
    """
    stmt = select(WebguiMessage).where(WebguiMessage.room_id == room_id)
    if since is not None:
        stmt = stmt.where(WebguiMessage.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(WebguiMessage.occurred_at <= until)
    if correlation_id is not None:
        stmt = stmt.where(WebguiMessage.correlation_id == correlation_id)
    # keyset 커서: (occurred_at, message_id) 튜플 비교
    if before is not None:
        bt, bid = before
        stmt = stmt.where(
            (WebguiMessage.occurred_at < bt)
            | ((WebguiMessage.occurred_at == bt) & (WebguiMessage.message_id < bid))
        )
    if after is not None:
        at, aid = after
        stmt = stmt.where(
            (WebguiMessage.occurred_at > at)
            | ((WebguiMessage.occurred_at == at) & (WebguiMessage.message_id > aid))
        )
    if direction == "desc":
        stmt = stmt.order_by(WebguiMessage.occurred_at.desc(), WebguiMessage.message_id.desc())
    else:
        stmt = stmt.order_by(WebguiMessage.occurred_at.asc(), WebguiMessage.message_id.asc())
    stmt = stmt.limit(limit + 1)
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
