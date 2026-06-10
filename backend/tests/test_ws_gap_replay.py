"""QI-WG-030: message-stream 재연결 gap replay.

연결 직후 after cursor 이후의 놓친 메시지를 replay 한다. replay envelope 는 실시간
push(collector/pm_bridge)와 동일 형태여야 FE 가 구분 없이 처리한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import repositories as repo
from app.db.base import Base
from app.routers.messages import _message_update_payload


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202
    return "JSON"


@pytest_asyncio.fixture
async def sm(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pg.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker, n: int):
    base = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    async with maker() as db:
        room = await repo.upsert_room(
            db, project_id="ProjA", role_id="DeveloperBE", display_name="BE",
            team_session_id="boot_1", agent_id="agent-1",
        )
        await db.commit()
        rid = room.room_id
        pid = room.project_id
    async with maker() as db:
        for i in range(n):
            await repo.create_message(
                db, room_id=rid, role_id="DeveloperBE", direction="inbound",
                source="transcript", message_type="assistant_message",
                normalized_text=f"msg{i:02d}", status="received",
                occurred_at=base + timedelta(minutes=i),
                recorded_at=base + timedelta(minutes=i),   # cursor 복구 검증용 명시
            )
        await db.commit()
    return rid, pid


@pytest.mark.asyncio
async def test_replay_envelope_matches_push_shape(sm):
    """replay envelope = 실시간 push 와 동일 형태 + DS-40 §6/§8 provenance/project_id."""
    rid, pid = await _seed(sm, 1)
    async with sm() as db:
        rows = await repo.updates_since(db, rid, None, 200)
    p = _message_update_payload(rows[0], pid)
    assert p["type"] == "message_update"
    assert "|message:" in p["cursor"]
    assert p["data"]["update_type"] == "message_received"
    msg = p["data"]["message"]
    assert msg["project_id"] == pid                       # QI-WG-029 공개키
    assert msg["provenance"]["origin"] == "transcript"
    assert msg["provenance"]["transport"] == "websocket"


@pytest.mark.asyncio
async def test_updates_since_replays_only_after_cursor(sm):
    """after cursor 이후 메시지만 replay 대상이다(놓친 구간만 복구)."""
    rid, _ = await _seed(sm, 5)
    async with sm() as db:
        all_rows = await repo.updates_since(db, rid, None, 200)
    assert len(all_rows) == 5
    cut = all_rows[1].recorded_at   # 두 번째 메시지 시각
    async with sm() as db:
        after_rows = await repo.updates_since(db, rid, cut, 200)
    # recorded_at > cut → 3,4,5 번째만(2개 이후 3개)
    assert [m.normalized_text for m in after_rows] == ["msg02", "msg03", "msg04"]
