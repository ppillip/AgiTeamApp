"""결함수정 2026-06-09: 유저→PM 메시지 SENT/LIVE TRANSCRIPT 이중 표시.

원인: transcript 의 user record 를 bridge 선저장본과 cross-source dedup 할 때
``normalized_text == text`` 정확 매칭이라 cmux 래핑/공백 차이로 매칭 실패 → 중복 insert.
수정: canonical(공백 정규화) 비교 + bridge 출처 한정.
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
from app.services.transcript_collector import (
    _find_outbound_text_dup,
    canonical_match_text,
)


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


async def _make_pm_room(db):
    room = await repo.upsert_room(
        db, project_id="WGDedup", role_id="PM", display_name="PM", room_type="pm",
    )
    await db.commit()
    return room


async def _prefile_bridge_msg(db, room, text: str):
    """pm_bridge 선저장본 흉내: source=webgui, direction=outbound."""
    msg = await repo.create_message(
        db,
        room_id=room.room_id,
        role_id="PM",
        direction="outbound",
        source="webgui",
        message_type="user_message",
        raw_text=text,
        normalized_text=text,
        status="pending",
        occurred_at=datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc),
    )
    await db.commit()
    return msg


def test_canonical_collapses_whitespace():
    assert canonical_match_text("hello   world") == "hello world"
    assert canonical_match_text("hello\nworld") == "hello world"
    assert canonical_match_text("  a\t b \n") == "a b"
    assert canonical_match_text(None) == ""


@pytest.mark.asyncio
async def test_dup_matches_despite_whitespace_diff(sessionmaker):
    """bridge 가 'hello world' 로 선저장, transcript 가 'hello  world\\n'(공백 차이) → 매칭되어야."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        bridge = await _prefile_bridge_msg(db, room, "hello world")
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, "hello  world\n")
        assert dup is not None
        assert dup.message_id == bridge.message_id


@pytest.mark.asyncio
async def test_exact_match_still_works(sessionmaker):
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        bridge = await _prefile_bridge_msg(db, room, "동일 텍스트")
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, "동일 텍스트")
        assert dup is not None and dup.message_id == bridge.message_id


@pytest.mark.asyncio
async def test_different_text_not_matched(sessionmaker):
    """정상 케이스: 다른 텍스트는 매칭 안 됨(중복 아님 → transcript insert 유지)."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        await _prefile_bridge_msg(db, room, "first message")
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, "completely different")
        assert dup is None


@pytest.mark.asyncio
async def test_transcript_source_not_matched(sessionmaker):
    """transcript 출처 outbound 는 bridge 가 아니므로 cross-source 매칭 대상이 아니다."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        await repo.create_message(
            db,
            room_id=room.room_id,
            role_id="PM",
            direction="outbound",
            source="transcript",
            message_type="user_message",
            raw_text="from transcript",
            normalized_text="from transcript",
            status="sent",
            occurred_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        )
        await db.commit()
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, "from transcript")
        assert dup is None


# --- 결함수정 2026-06-14: 이미지 첨부 합성본([Image: source:]) 중복 저장 ----------

async def _prefile_bridge_attachment_msg(db, room, text: str):
    """이미지 첨부 bridge 선저장본: source=webgui, attachments_json 보유, 공개 text=원문."""
    msg = await repo.create_message(
        db,
        room_id=room.room_id,
        role_id="PM",
        direction="outbound",
        source="webgui",
        message_type="user_message",
        raw_text=text,
        normalized_text=text,
        attachments_json=[{"kind": "image", "attachment_id": "att-1"}],
        status="pending",
        occurred_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )
    await db.commit()
    return msg


@pytest.mark.asyncio
async def test_image_attachment_synth_text_matches_bridge(sessionmaker):
    """버그 재현: bridge 가 원문만 저장, transcript 가 원문+[Image: source:] 합성본 → 매칭되어 skip."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        bridge = await _prefile_bridge_attachment_msg(db, room, "저 물음표 머지? 문제 생긴건가?")
    synth = "저 물음표 머지? 문제 생긴건가?\n\n[Image: source: /Users/ppillip/Projects/Panthea/.agiteam/webgui/uploads/images/x.png]"
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, synth)
        assert dup is not None and dup.message_id == bridge.message_id


@pytest.mark.asyncio
async def test_codex_attachment_block_matches_bridge(sessionmaker):
    """codex 합성형식(첨부 이미지 파일 경로:)도 본문만으로 매칭되어야."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        bridge = await _prefile_bridge_attachment_msg(db, room, "이 화면 봐줘")
    synth = "이 화면 봐줘\n\n첨부 이미지 파일 경로:\n/abs/a.png\n/abs/b.jpg"
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, synth)
        assert dup is not None and dup.message_id == bridge.message_id


@pytest.mark.asyncio
async def test_image_only_empty_text_matches_bridge(sessionmaker):
    """본문 없이 이미지만 전송: bridge text='' + 첨부 보유, transcript=sentinel+[Image:source:] → 매칭."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        bridge = await _prefile_bridge_attachment_msg(db, room, "")
    synth = "첨부 이미지를 확인하세요.\n\n[Image: source: /abs/only.png]"
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, synth)
        assert dup is not None and dup.message_id == bridge.message_id


@pytest.mark.asyncio
async def test_text_only_still_single(sessionmaker):
    """텍스트만 전송(첨부 없음)은 현행대로 정확 매칭 — 합성 strip 이 본문을 훼손하지 않는다."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        bridge = await _prefile_bridge_msg(db, room, "그냥 텍스트 메시지")
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, "그냥 텍스트 메시지")
        assert dup is not None and dup.message_id == bridge.message_id


@pytest.mark.asyncio
async def test_different_image_message_not_matched(sessionmaker):
    """다른 본문의 이미지 첨부 메시지는 매칭되면 안 된다(과매칭 방지)."""
    async with sessionmaker() as db:
        room = await _make_pm_room(db)
        await _prefile_bridge_attachment_msg(db, room, "첫 번째 이미지 메시지")
    synth = "완전히 다른 메시지\n\n[Image: source: /abs/other.png]"
    async with sessionmaker() as db:
        dup = await _find_outbound_text_dup(db, room.room_id, synth)
        assert dup is None
