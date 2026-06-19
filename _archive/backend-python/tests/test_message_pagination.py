"""DV-41: 메시지 페이지네이션(limit=20 + before cursor) + provenance/team_session_id 검증."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import repositories as repo
from app.db.base import Base
from app.db.serializers import message_to_dict, provenance_dict


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


async def _seed(sm, n: int):
    base = datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc)
    async with sm() as db:
        room = await repo.upsert_room(
            db, project_id="HookTest", role_id="DeveloperBE", display_name="BE",
            team_session_id="boot_1", agent_id="agent-1",
        )
        await db.commit()
        rid = room.room_id
    async with sm() as db:
        for i in range(n):
            await repo.create_message(
                db, room_id=rid, role_id="DeveloperBE", direction="inbound",
                source="transcript", message_type="assistant_message",
                team_session_id="boot_1", normalized_text=f"msg{i:02d}",
                status="received", occurred_at=base + timedelta(minutes=i),
            )
        await db.commit()
    return rid


@pytest.mark.asyncio
async def test_pagination_limit20_then_before_cursor(sessionmaker):
    rid = await _seed(sessionmaker, 25)
    # 1페이지: 최신 20개 (desc). limit+1 fetch 로 has_more 판정
    async with sessionmaker() as db:
        rows = await repo.list_messages(db, rid, limit=20, direction="desc")
    has_more = len(rows) > 20
    rows = rows[:20]
    assert has_more is True
    assert len(rows) == 20
    assert rows[0].normalized_text == "msg24"   # 최신
    assert rows[-1].normalized_text == "msg05"  # 페이지 끝(가장 오래된 표시)
    # next_cursor = 페이지의 가장 오래된 메시지
    before = (rows[-1].occurred_at, rows[-1].message_id)

    # 2페이지: before 커서로 과거 5개
    async with sessionmaker() as db:
        older = await repo.list_messages(db, rid, limit=20, direction="desc", before=before)
    has_more2 = len(older) > 20
    older = older[:20]
    assert has_more2 is False
    assert [m.normalized_text for m in older] == [f"msg{i:02d}" for i in range(4, -1, -1)]


@pytest.mark.asyncio
async def test_message_dict_provenance_and_team_session(sessionmaker):
    rid = await _seed(sessionmaker, 1)
    async with sessionmaker() as db:
        rows = await repo.list_messages(db, rid, limit=20, direction="desc")
    d = message_to_dict(rows[0], runtime_state="live", project_id="HookTest")
    assert d["team_session_id"] == "boot_1"
    assert d["project_id"] == "HookTest"               # QI-WG-029: DS-40 §8 공개키
    # QI-WG-029: provenance 는 DS-40 §6 형태(origin/runtime_state/is_real_data/is_mock)
    assert d["provenance"]["origin"] == "transcript"
    assert d["provenance"]["is_real_data"] is True
    assert d["provenance"]["is_mock"] is False
    assert d["provenance"]["runtime_state"] == "live"


@pytest.mark.asyncio
async def test_ws_payload_is_json_serializable(sessionmaker):
    """QI-WG-026 회귀가드: WS publish payload(메시지 본문)는 datetime 을 포함하므로
    ws.send_json 의 기본 json.dumps 로는 실패한다. jsonable_encoder 로 직렬화해야 한다."""
    import json
    from fastapi.encoders import jsonable_encoder

    rid = await _seed(sessionmaker, 1)
    async with sessionmaker() as db:
        rows = await repo.list_messages(db, rid, limit=1, direction="desc")
    msg = rows[0]
    payload = {
        "type": "message_update",
        "cursor": f"{msg.recorded_at.isoformat()}|message:{msg.message_id}",
        "data": {
            "update_type": "message_received",
            "room_id": str(msg.room_id),
            "message": message_to_dict(msg),   # occurred_at/recorded_at/updated_at = datetime
            "occurred_at": msg.occurred_at.isoformat(),
        },
    }
    # 기존 버그: 순수 json.dumps 는 datetime 에서 실패 → ws.send_json 이 조용히 유실시켰음
    with pytest.raises(TypeError):
        json.dumps(payload)
    # 수정: jsonable_encoder 통과 후 직렬화 성공 (WS 전송 경로와 동일)
    encoded = jsonable_encoder(payload)
    s = json.dumps(encoded)
    assert "message_received" in s and str(msg.message_id) in s


def test_provenance_kinds():
    # QI-WG-029: DS-40 §6 형태 — origin/runtime_state/is_real_data/is_mock/transport
    assert provenance_dict("hook")["origin"] == "hook"
    assert provenance_dict("hook")["is_real_data"] is True
    assert provenance_dict("transcript")["is_real_data"] is True
    # webgui(사용자 실제 입력)도 실데이터 (DS-40 §6: "...webgui...면 true")
    assert provenance_dict("webgui")["is_real_data"] is True
    assert provenance_dict("webgui")["is_mock"] is False
    # mock/None source → is_mock True + runtime_state mock 강제
    assert provenance_dict("mock")["runtime_state"] == "mock"
    assert provenance_dict("mock")["is_mock"] is True
    assert provenance_dict(None)["is_mock"] is True
    assert provenance_dict("transcript", runtime_state="disconnected")["runtime_state"] == "disconnected"
    # transport 선택 필드
    assert provenance_dict("pm_bridge", transport="rest")["transport"] == "rest"
    assert "transport" not in provenance_dict("hook")
