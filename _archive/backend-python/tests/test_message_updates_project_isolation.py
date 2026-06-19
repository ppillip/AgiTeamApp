"""A-F1 결함수정 2026-06-10: GET /message-updates project_id 격리 방어검증.

아테나 판정: 설계 의도는 room_id UUID 신뢰 완화가 아니라, API 경계에서 project_id
방어 검증을 강제하는 것. cross-project room_id 로 조회 시 정보 은닉을 위해
room_not_found(404)로 거절한다(존재 여부 비노출, DS-40 §21).
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import repositories as repo
from app.db.base import Base
from app.errors import WebguiError
from app.routers.messages import message_updates


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202
    return "JSON"


@pytest_asyncio.fixture
async def sessionmaker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pg.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _seed_room(sm, project_id: str):
    async with sm() as db:
        room = await repo.upsert_room(
            db, project_id=project_id, role_id="DeveloperBE", display_name="BE",
            team_session_id="boot_1", agent_id="agent-1",
        )
        await repo.create_message(
            db, room_id=room.room_id, role_id="DeveloperBE", direction="inbound",
            source="transcript", message_type="assistant_message",
            normalized_text="hello", status="received",
            occurred_at=datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc),
        )
        await db.commit()
        return room.room_id


@pytest.mark.asyncio
async def test_matching_project_returns_updates(sessionmaker):
    """올바른 project_id 면 정상적으로 updates 를 돌려준다."""
    rid = await _seed_room(sessionmaker, "ProjA")
    async with sessionmaker() as db:
        env = await message_updates(room_id=str(rid), project_id="ProjA", after=None, limit=50, db=db)
    assert env["ok"] is True
    assert len(env["data"]["updates"]) == 1


@pytest.mark.asyncio
async def test_cross_project_is_hidden_404(sessionmaker):
    """다른 project_id 로 남의 방 room_id 를 조회하면 room_not_found(404)로 은닉 거절."""
    rid = await _seed_room(sessionmaker, "ProjA")
    async with sessionmaker() as db:
        with pytest.raises(WebguiError) as ei:
            await message_updates(room_id=str(rid), project_id="ProjB", after=None, limit=50, db=db)
    assert ei.value.http_status == 404
    assert ei.value.code == "room_not_found"


@pytest.mark.asyncio
async def test_unknown_room_404(sessionmaker):
    """존재하지 않는 room_id 도 동일하게 404 (기존 거동 유지)."""
    async with sessionmaker() as db:
        with pytest.raises(WebguiError) as ei:
            await message_updates(
                room_id=str(_uuid.uuid4()), project_id="ProjA", after=None, limit=50, db=db
            )
    assert ei.value.http_status == 404
    assert ei.value.code == "room_not_found"
