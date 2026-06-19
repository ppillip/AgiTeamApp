"""hook_stop → 해당 에이전트 세션 transcript 즉시 수집 트리거 통합테스트 (DV-25 정정).

검증:
1. hook_stop 1회 수신 → 그 세션 신규 턴의 질문(user)/답변(assistant) 말풍선 N건 저장.
2. 중복 hook_stop → offset/record dedupe 로 추가 저장 0.
3. 방 라우팅 절대 원칙(유저 확정 2026-06-08): AGENT_ID 1차 키 → 동종 CLI 다중 구동 시에도
   각 에이전트 말풍선이 자기 방(room_id)에만 저장되고 서로 섞이지 않는다.

DB 는 인메모리/파일 SQLite 로 격리한다. JSONB 컬럼은 SQLite 용으로 JSON 으로 컴파일한다
(테스트 전용 shim — runtime 은 PostgreSQL).
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import models
from app.db.base import Base
from app.db.models import WebguiMessage
from app.schemas.collector import CollectEventRequest, HookCollectRequest
from app.services import collector_service
from app.services.cmux_discovery import DiscoveryRegistry
from app.services.transcript_collector import (
    TranscriptCollector,
    TranscriptSessionRegistry,
)
from app.services.transcript_parser import PROVIDER_CLAUDE


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202
    """SQLite 에는 JSONB 가 없으므로 JSON 으로 렌더 (테스트 전용)."""
    return "JSON"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_claude_turn(path, *, session_id: str, user_uuid: str, asst_uuid: str,
                       question: str, answer: str) -> None:
    import json

    lines = [
        json.dumps({
            "type": "user", "uuid": user_uuid, "timestamp": "2026-06-08T01:00:00Z",
            "sessionId": session_id, "cwd": "/x",
            "message": {"role": "user", "content": question},
        }),
        json.dumps({
            "type": "assistant", "uuid": asst_uuid, "timestamp": "2026-06-08T01:00:05Z",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        }),
    ]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@pytest_asyncio.fixture
async def sessionmaker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def make_room(sessionmaker):
    from app.db import repositories as repo

    async def _make(role_id: str):
        async with sessionmaker() as db:
            room = await repo.upsert_room(
                db, project_id="Panthea", role_id=role_id,
                display_name=role_id, room_type="role",
            )
            await db.commit()
            return str(room.room_id)

    return _make


def _collector(sessionmaker) -> TranscriptCollector:
    # 매 테스트 독립 레지스트리 → 전역 singleton 오염 방지
    return TranscriptCollector(
        settings=__import__("app.config", fromlist=["get_settings"]).get_settings(),
        registry=DiscoveryRegistry(),
        sessionmaker=sessionmaker,
        session_registry=TranscriptSessionRegistry(),
    )


def _hook_stop_body(*, session_id: str, transcript_path, agent_id: str) -> CollectEventRequest:
    return CollectEventRequest(
        source="hook",
        hook_provider=PROVIDER_CLAUDE,
        hook_event_name="Stop",
        payload={
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "agent_id": agent_id,
        },
        occurred_at=_now(),
    )


async def _count_messages(sessionmaker, room_id: str) -> int:
    async with sessionmaker() as db:
        res = await db.execute(
            select(func.count()).select_from(WebguiMessage)
            .where(WebguiMessage.room_id == _uuid.UUID(room_id))
        )
        return int(res.scalar_one())


async def _messages(sessionmaker, room_id: str):
    async with sessionmaker() as db:
        res = await db.execute(
            select(WebguiMessage).where(WebguiMessage.room_id == _uuid.UUID(room_id))
            .order_by(WebguiMessage.occurred_at.asc())
        )
        return list(res.scalars().all())


@pytest.mark.asyncio
async def test_hook_stop_collects_new_turn(sessionmaker, make_room, tmp_path):
    """hook_stop 1회 → 질문/답변 말풍선 2건 저장, 재발신은 dedupe 로 0."""
    room_id = await make_room("DeveloperBE")
    collector = _collector(sessionmaker)
    transcript = tmp_path / "sessHook.jsonl"
    _write_claude_turn(transcript, session_id="sessHook", user_uuid="u1", asst_uuid="a1",
                       question="질문입니다", answer="답변입니다")

    body = _hook_stop_body(session_id="sessHook", transcript_path=transcript, agent_id="claude-be-1")

    async with sessionmaker() as db:
        await collector_service.collect_event(db, room_id, body, collector=collector)

    msgs = await _messages(sessionmaker, room_id)
    assert len(msgs) == 2, f"질문/답변 말풍선 2건이 저장돼야 함, got {len(msgs)}"
    kinds = {(m.direction, m.message_type) for m in msgs}
    assert ("outbound", "user_message") in kinds      # 질문 = 별도 말풍선
    assert ("inbound", "assistant_message") in kinds or ("inbound", "unmatched") in kinds  # 답변 = 별도 말풍선
    texts = {m.normalized_text for m in msgs}
    assert "질문입니다" in texts and "답변입니다" in texts

    # 같은 hook_stop 재수신 → 신규 record 없음(offset + record_id dedupe)
    async with sessionmaker() as db:
        body2 = _hook_stop_body(session_id="sessHook", transcript_path=transcript, agent_id="claude-be-1")
        await collector_service.collect_event(db, room_id, body2, collector=collector)
    assert await _count_messages(sessionmaker, room_id) == 2


@pytest.mark.asyncio
async def test_hook_stop_collects_incremental_turn(sessionmaker, make_room, tmp_path):
    """첫 hook_stop 이후 새 턴이 append 되면 다음 hook_stop 이 그 신규분만 수집."""
    room_id = await make_room("DeveloperBE")
    collector = _collector(sessionmaker)
    transcript = tmp_path / "sessHook.jsonl"
    _write_claude_turn(transcript, session_id="sessHook", user_uuid="u1", asst_uuid="a1",
                       question="첫질문", answer="첫답변")
    body = _hook_stop_body(session_id="sessHook", transcript_path=transcript, agent_id="claude-be-1")
    async with sessionmaker() as db:
        await collector_service.collect_event(db, room_id, body, collector=collector)
    assert await _count_messages(sessionmaker, room_id) == 2

    # 새 턴 append → 다음 hook_stop
    _write_claude_turn(transcript, session_id="sessHook", user_uuid="u2", asst_uuid="a2",
                       question="둘째질문", answer="둘째답변")
    async with sessionmaker() as db:
        body2 = _hook_stop_body(session_id="sessHook", transcript_path=transcript, agent_id="claude-be-1")
        await collector_service.collect_event(db, room_id, body2, collector=collector)
    assert await _count_messages(sessionmaker, room_id) == 4
    texts = {m.normalized_text for m in await _messages(sessionmaker, room_id)}
    assert {"첫질문", "첫답변", "둘째질문", "둘째답변"} <= texts


@pytest.mark.asyncio
async def test_agent_id_routing_isolation(sessionmaker, make_room, tmp_path):
    """절대 원칙: 두 에이전트(서로 다른 AGENT_ID/room)는 절대 한 방에 섞이지 않는다."""
    room_a = await make_room("DeveloperBE")
    room_b = await make_room("PM")
    collector = _collector(sessionmaker)  # 동일 collector/레지스트리로 동시 구동 모사

    ta = tmp_path / "sessA.jsonl"
    tb = tmp_path / "sessB.jsonl"
    _write_claude_turn(ta, session_id="sessA", user_uuid="ua", asst_uuid="aa",
                       question="A질문", answer="A답변")
    _write_claude_turn(tb, session_id="sessB", user_uuid="ub", asst_uuid="ab",
                       question="B질문", answer="B답변")

    async with sessionmaker() as db:
        await collector_service.collect_event(
            db, room_a,
            _hook_stop_body(session_id="sessA", transcript_path=ta, agent_id="claude-be-1"),
            collector=collector,
        )
    async with sessionmaker() as db:
        await collector_service.collect_event(
            db, room_b,
            _hook_stop_body(session_id="sessB", transcript_path=tb, agent_id="claude-pm-1"),
            collector=collector,
        )

    a_texts = {m.normalized_text for m in await _messages(sessionmaker, room_a)}
    b_texts = {m.normalized_text for m in await _messages(sessionmaker, room_b)}
    assert a_texts == {"A질문", "A답변"}, f"room A 에 A 턴만 있어야 함: {a_texts}"
    assert b_texts == {"B질문", "B답변"}, f"room B 에 B 턴만 있어야 함: {b_texts}"
    # 교차 오염 없음
    assert "B질문" not in a_texts and "A질문" not in b_texts


async def _room_id_of(sessionmaker, project_id: str, role_id: str) -> str:
    from app.db import repositories as repo

    async with sessionmaker() as db:
        room = await repo.get_room_by_role(db, project_id, role_id)
        return str(room.room_id) if room else ""


async def _room_of(sessionmaker, room_id: str):
    from app.db import repositories as repo

    async with sessionmaker() as db:
        return await repo.get_room(db, room_id)


async def _count_rooms(sessionmaker, project_id: str, role_id: str) -> int:
    from app.db.models import WebguiRoom

    async with sessionmaker() as db:
        res = await db.execute(
            select(func.count()).select_from(WebguiRoom).where(
                WebguiRoom.project_id == project_id, WebguiRoom.role_id == role_id
            )
        )
        return int(res.scalar_one())


@pytest.mark.asyncio
async def test_distinct_projects_with_rooms(sessionmaker):
    """QI-WG-021: 방을 보유한 project_id 집계 — UI 프로젝트 목록 도달 보장."""
    from app.db import repositories as repo

    async with sessionmaker() as db:
        await repo.upsert_room(db, project_id="HookTest", role_id="PM",
                               display_name="PM", room_type="pm")
        await repo.upsert_room(db, project_id="HookTest", role_id="DeveloperBE",
                               display_name="BE", room_type="role")
        await repo.upsert_room(db, project_id="Panthea", role_id="PM",
                               display_name="PM", room_type="pm")
        await db.commit()

    async with sessionmaker() as db:
        rows = await repo.distinct_projects_with_rooms(db)

    by_id = {r["project_id"]: r for r in rows}
    assert set(by_id) == {"HookTest", "Panthea"}
    assert by_id["HookTest"]["room_count"] == 2
    assert set(by_id["HookTest"]["roles"]) == {"PM", "DeveloperBE"}
    assert by_id["Panthea"]["room_count"] == 1


@pytest.mark.asyncio
async def test_hook_collect_agent_routing_upserts_room(sessionmaker, tmp_path):
    """AGENT_ID 라우팅 엔드포인트: room_id 없이 hook → (project_id,role) 방 upsert → 질문/답변 저장."""
    collector = _collector(sessionmaker)
    transcript = tmp_path / "sessZ.jsonl"
    _write_claude_turn(transcript, session_id="sessZ", user_uuid="z1", asst_uuid="z2",
                       question="라우팅질문", answer="라우팅답변")

    body = HookCollectRequest(
        project_id="Panthea",
        team_session_id="20260608_100000",
        agent_id="claude-be-9",
        role="DeveloperBE",
        cli="claude",                    # → provider claude_code 정규화
        hook_event_name="Stop",
        session_id="sessZ",
        transcript_path=str(transcript),
        occurred_at=_now(),
    )

    async with sessionmaker() as db:
        result = await collector_service.collect_hook(db, body, collector=collector)

    # 방이 새로 생성되고 result 에 room_id 가 실린다
    assert result.get("room_id")
    room_id = await _room_id_of(sessionmaker, "Panthea", "DeveloperBE")
    assert room_id and room_id == result["room_id"]
    # 안정키(team_session_id, agent_id) 가 방에 바인딩된다
    bound = await _room_of(sessionmaker, room_id)
    assert bound.team_session_id == "20260608_100000" and bound.agent_id == "claude-be-9"

    texts = {m.normalized_text for m in await _messages(sessionmaker, room_id)}
    assert texts == {"라우팅질문", "라우팅답변"}, f"질문/답변 말풍선이 그 방에 저장돼야 함: {texts}"

    # 같은 에이전트 재발신(증분 없음) → dedupe 로 추가 0, 방은 재upsert(중복 생성 안 됨)
    async with sessionmaker() as db:
        body2 = HookCollectRequest(
            project_id="Panthea", team_session_id="20260608_100000",
            agent_id="claude-be-9", role="DeveloperBE", cli="claude",
            hook_event_name="Stop", session_id="sessZ", transcript_path=str(transcript),
            occurred_at=_now(),
        )
        await collector_service.collect_hook(db, body2, collector=collector)
    assert await _count_messages(sessionmaker, room_id) == 2


@pytest.mark.asyncio
async def test_hook_collect_round2_contract_body(sessionmaker, tmp_path):
    """HOOK 계약 round2 §2 body 형태 그대로 수용: hook_provider top-level, hook_stdin 보강,
    hook_event_name 미지정(→Stop). project_id+team_session_id+agent_id+role 로 방 upsert."""
    collector = _collector(sessionmaker)
    transcript = tmp_path / "contract.jsonl"
    _write_claude_turn(transcript, session_id="sessC", user_uuid="c1", asst_uuid="c2",
                       question="계약Q", answer="계약A")

    # 런처/log_stop.sh 가 보내는 실제 JSON body (dict → 모델 파싱; extra 무시)
    raw_body = {
        "project_id": "Panthea",
        "team_session_id": "20260608_120000",
        "agent_id": "claude-be-5",
        "role": "DeveloperBE",
        "hook_provider": "claude_code",          # cli 아님 — 계약 §2 필드명
        "occurred_at": _now().isoformat(),
        "transcript_path": str(transcript),
        "hook_stdin": {"session_id": "sessC"},   # payload 아님 — session_id 는 보강에서 추출
        # hook_event_name 없음 → 기본 Stop
    }
    body = HookCollectRequest(**raw_body)
    assert body.hook_event_name == "Stop"

    async with sessionmaker() as db:
        result = await collector_service.collect_hook(db, body, collector=collector)

    room_id = result["room_id"]
    bound = await _room_of(sessionmaker, room_id)
    assert bound.team_session_id == "20260608_120000" and bound.agent_id == "claude-be-5"
    texts = {m.normalized_text for m in await _messages(sessionmaker, room_id)}
    assert texts == {"계약Q", "계약A"}, f"hook_stdin.session_id 로 transcript 수집돼야 함: {texts}"


@pytest.mark.asyncio
async def test_hook_collect_reboot_reuses_same_room(sessionmaker, tmp_path):
    """QI-WG-022: 재부팅(team_session_id 변경)이어도 (project, role) 방은 1개로 유지된다.
    방 증식 금지. team_session_id 는 현재 세션 provenance 로 갱신된다."""
    collector = _collector(sessionmaker)
    t1 = tmp_path / "s1.jsonl"
    _write_claude_turn(t1, session_id="s1", user_uuid="r1", asst_uuid="r2",
                       question="구턴Q", answer="구턴A")
    t2 = tmp_path / "s2.jsonl"
    _write_claude_turn(t2, session_id="s2", user_uuid="n1", asst_uuid="n2",
                       question="신턴Q", answer="신턴A")

    common = dict(project_id="Panthea", agent_id="claude-be-7", role="DeveloperBE",
                  cli="claude", hook_event_name="Stop")
    async with sessionmaker() as db:
        r1 = await collector_service.collect_hook(
            db, HookCollectRequest(team_session_id="boot_A", session_id="s1",
                                   transcript_path=str(t1), occurred_at=_now(), **common),
            collector=collector)
    async with sessionmaker() as db:
        r2 = await collector_service.collect_hook(
            db, HookCollectRequest(team_session_id="boot_B", session_id="s2",
                                   transcript_path=str(t2), occurred_at=_now(), **common),
            collector=collector)

    assert r1["room_id"] == r2["room_id"], "재부팅에도 같은 (project,role) 방을 재사용해야 함"
    assert await _count_rooms(sessionmaker, "Panthea", "DeveloperBE") == 1
    room = await _room_of(sessionmaker, r2["room_id"])
    assert room.team_session_id == "boot_B", "team_session_id 는 최신 세션 provenance 로 갱신"
    # 두 부팅의 말풍선이 한 방에 누적
    texts = {m.normalized_text for m in await _messages(sessionmaker, r1["room_id"])}
    assert texts == {"구턴Q", "구턴A", "신턴Q", "신턴A"}


@pytest.mark.asyncio
async def test_hook_collect_same_role_different_agent_same_room(sessionmaker, tmp_path):
    """같은 role 에 다른 agent_id 가 와도 (project, role) 방은 1개. agent_id 는 provenance 갱신만."""
    collector = _collector(sessionmaker)
    t = tmp_path / "m.jsonl"
    _write_claude_turn(t, session_id="sm", user_uuid="m1", asst_uuid="m2",
                       question="Q", answer="A")
    base = dict(project_id="Panthea", team_session_id="boot_X",
                cli="claude", hook_event_name="Stop", session_id="sm",
                transcript_path=str(t), role="DeveloperBE")
    async with sessionmaker() as db:
        r1 = await collector_service.collect_hook(
            db, HookCollectRequest(agent_id="agent-1", occurred_at=_now(), **base),
            collector=collector)
    async with sessionmaker() as db:
        r2 = await collector_service.collect_hook(
            db, HookCollectRequest(agent_id="agent-2", occurred_at=_now(), **base),
            collector=collector)
    assert r1["room_id"] == r2["room_id"]
    assert await _count_rooms(sessionmaker, "Panthea", "DeveloperBE") == 1
    room = await _room_of(sessionmaker, r2["room_id"])
    assert room.agent_id == "agent-2", "agent_id 는 최신 provenance 로 갱신"
