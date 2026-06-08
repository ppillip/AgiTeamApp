"""WG 결함수정: WS broadcast message payload 에 client_message_id 포함 검증.

증상: 웹GUI 송신 시 말풍선이 2개로 중복 표시(새로고침하면 1개로 합쳐짐).
근본원인: broadcast(message_update) 의 data.message 에 client_message_id 가 없어
프론트가 낙관적(optimistic) 말풍선과 서버 말풍선을 상관(correlate)하지 못함.
본 테스트는 send() 의 broadcast message 와 반환 message 가 dedup 키
(client_message_id)를 싣는지 회귀 검증한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.config import Settings
from app.db.base import Base
from app.services.cmux_discovery import DiscoveryRegistry, SurfaceInfo
from app.services.pm_bridge import PMBridge
from app.services import events as events_mod


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


class _FakeAdapter:
    """cmux 호출을 모두 성공으로 흉내내는 가짜 adapter (네트워크 비의존)."""

    async def tree(self):
        return None  # _refresh_discovery 를 no-op 으로 (registry 주입값 유지)

    async def runtime_metadata(self, tree):
        return {}

    async def read_screen(self, surface_id, *, lines=1, workspace_id="", tty=None):
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def submit(self, surface_id, text, workspace_id, tty):
        return {"submitted": True, "ended_at": "2026-06-08T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_broadcast_message_carries_client_message_id(sessionmaker, monkeypatch):
    project_id = "WGDedup"
    cmid = "client-abc-123"

    registry = DiscoveryRegistry()
    registry._map[(project_id, "PM")] = SurfaceInfo(  # noqa: SLF001
        project_id=project_id,
        role_id="PM",
        surface_id="surface:01",
        display_name="PM",
        connection_state="connected",
        last_seen_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        workspace_id="ws-1",
        tty="ttys001",
    )

    bridge = PMBridge(Settings(), adapter=_FakeAdapter(), registry=registry)

    captured: list[dict] = []

    async def _capture(room_id, payload):
        captured.append(payload)

    monkeypatch.setattr(events_mod.hub, "publish", _capture)

    async with sessionmaker() as db:
        result = await bridge.send(
            db, project_id=project_id, text="hello", client_message_id=cmid,
        )

    # 반환 message 에 dedup 키 존재
    assert result["message"]["client_message_id"] == cmid
    assert result["ack"]["client_message_id"] == cmid

    # broadcast(message_update) 의 data.message 에도 동일 dedup 키 존재 (핵심 회귀)
    assert len(captured) == 1
    broadcast_message = captured[0]["data"]["message"]
    assert broadcast_message["client_message_id"] == cmid
    # ack 와 broadcast 가 같은 키를 써야 프론트가 상관 가능
    assert broadcast_message["client_message_id"] == result["ack"]["client_message_id"]
