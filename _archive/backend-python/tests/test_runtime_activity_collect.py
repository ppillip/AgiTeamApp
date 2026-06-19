"""요구사항 15-1 read-screen poller active pulse 수신/WS fanout 단위테스트 (DS-110 §6·§7·§12.1).

[2026-06-15 설계정정] liveness 는 휘발성 — **DB 를 일절 쓰지 않는다**. 따라서 본 테스트도
DB 비의존이다: 인메모리 activity_registry 갱신 + WS publish + dedup 만 검증한다.

검증(폴러 없이 collect_runtime_activity 를 직접 호출 = curl 모사):
1. POST 1건(active) → registry pulse 갱신 + WS runtime_activity_changed 1건. **DB write 없음**.
2. 같은 snapshot_hash 재POST → idempotent 무시(WS 추가 0).
3. activity != active → 422 invalid_activity.
4. Monitor role → 422 invalid_role.
5. 새 hash 마다 active pulse 만 — 서버는 idle 을 발행하지 않는다(§3.2/§6.2.7).

WS 는 실제 전역 hub 에 구독자를 등록해 publish 를 포착한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app import errors
from app.schemas.collector import RuntimeActivityCollectRequest
from app.services import runtime_activity_service as svc
from app.services.events import hub
from app.services.log_collector import RUNTIME_ACTIVITY_EVENT, activity_registry


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean_registry():
    """전역 activity_registry 오염 차단 — 각 테스트 전후 초기화."""
    activity_registry._pulse.clear()
    activity_registry._map.clear()
    yield
    activity_registry._pulse.clear()
    activity_registry._map.clear()


@pytest_asyncio.fixture
async def ws_queue():
    """전역 hub 에 Panthea 구독자 등록 → publish 포착. 테스트 후 해제."""
    q = await hub.register(None, "Panthea")
    yield q
    await hub.unregister(q)


def _body(*, role="Architect", activity="active", snapshot_hash="sha256:aaa",
          observed_at=None, surface_id="surface:02", **kw) -> RuntimeActivityCollectRequest:
    return RuntimeActivityCollectRequest(
        project_id="Panthea",
        team_session_id="20260615_010000",
        role=role,
        display_name=kw.pop("display_name", role),
        surface_id=surface_id,
        activity=activity,
        reason="read_screen_changed",
        snapshot_hash=snapshot_hash,
        snapshot_bytes=kw.pop("snapshot_bytes", 8421),
        poll_interval_ms=1000,
        observed_at=observed_at or _now(),
        agiteam_id_path=".agiteam/agiteam.id",
        schema_version=1,
        **kw,
    )


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.mark.asyncio
async def test_active_pulse_updates_registry_and_ws(ws_queue):
    """POST 1건 → registry pulse 갱신 + WS runtime_activity_changed 1건. DB 미접근."""
    result = await svc.collect_runtime_activity(_body(snapshot_hash="sha256:h1"))

    assert result["accepted"] is True and result["deduplicated"] is False
    assert result["runtime_activity"] == "active"
    assert result["event_id"] and "room_id" not in result  # DB room 없음

    # 인메모리 registry pulse 갱신
    p = activity_registry.pulse("Panthea", "Architect")
    assert p is not None
    assert p.runtime_activity == "active"
    assert p.last_activity_hash == "sha256:h1"
    assert p.last_active_at is not None

    # WS: 정확히 1건, §7 스키마, raw screen 미포함
    msgs = _drain(ws_queue)
    assert len(msgs) == 1
    data = msgs[0]["data"]
    assert data["update_type"] == RUNTIME_ACTIVITY_EVENT
    assert data["room_id"] is None                       # liveness 는 room 안 만듦
    assert data["event"]["source"] == "read_screen_poller"
    assert data["event"]["payload"]["runtime_activity"] == "active"
    assert data["event"]["payload"]["snapshot_hash"] == "sha256:h1"
    assert "sample" not in data["event"]["payload"]      # 원문 비포함


@pytest.mark.asyncio
async def test_duplicate_hash_ignored(ws_queue):
    """같은 snapshot_hash 재POST → WS 추가 0 (idempotent)."""
    await svc.collect_runtime_activity(_body(snapshot_hash="sha256:dup"))
    result2 = await svc.collect_runtime_activity(_body(snapshot_hash="sha256:dup"))

    assert result2["deduplicated"] is True
    assert result2["event_id"] is None
    assert len(_drain(ws_queue)) == 1                     # WS 도 1건뿐


@pytest.mark.asyncio
async def test_changed_hash_emits_again(ws_queue):
    """hash 가 바뀌면 다시 active pulse 1건 발행."""
    await svc.collect_runtime_activity(_body(snapshot_hash="sha256:s1"))
    await svc.collect_runtime_activity(_body(snapshot_hash="sha256:s2"))
    msgs = _drain(ws_queue)
    assert len(msgs) == 2
    assert all(m["data"]["event"]["payload"]["runtime_activity"] == "active" for m in msgs)
    # 어떤 pulse 도 idle 을 싣지 않는다(서버발 idle 없음)
    assert not any(m["data"]["event"]["payload"]["runtime_activity"] == "idle" for m in msgs)


@pytest.mark.asyncio
async def test_non_active_rejected(ws_queue):
    """activity != active → 422 (서버는 비-active 를 받지 않는다, §6.2.1)."""
    with pytest.raises(errors.WebguiError) as ei:
        await svc.collect_runtime_activity(_body(activity="idle"))
    assert ei.value.http_status == 422 and ei.value.code == "invalid_activity"
    assert len(_drain(ws_queue)) == 0
    assert activity_registry.pulse("Panthea", "Architect") is None


@pytest.mark.asyncio
async def test_monitor_role_excluded(ws_queue):
    """Monitor pane 은 활동 대상 제외 → 422 invalid_role (§12.1)."""
    with pytest.raises(errors.WebguiError) as ei:
        await svc.collect_runtime_activity(_body(role="Monitor", surface_id="surface:08"))
    assert ei.value.http_status == 422 and ei.value.code == "invalid_role"
    assert len(_drain(ws_queue)) == 0
    assert activity_registry.pulse("Panthea", "Monitor") is None
