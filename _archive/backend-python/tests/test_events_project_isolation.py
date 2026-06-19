"""A-F1 후속 2026-06-10: WebSocketHub 프로젝트 격리 필터.

message-stream 의 room_id 없는 전역구독(rooms=None)이 hub 에서 모든 프로젝트의
push 를 받던 누수를 차단한다. 구독자가 project_id 를 지정하면 그 프로젝트 push 만 받는다.
"""
from __future__ import annotations

import pytest

from app.services.events import WebSocketHub


@pytest.mark.asyncio
async def test_project_isolation_blocks_cross_project():
    """전역구독(rooms=None)이라도 타 프로젝트 push 는 받지 않는다."""
    hub = WebSocketHub()
    qa = await hub.register(None, "ProjA")
    qb = await hub.register(None, "ProjB")
    await hub.publish("room-1", {"x": 1}, project_id="ProjA")
    assert qa.qsize() == 1   # 자기 프로젝트 수신
    assert qb.qsize() == 0   # cross-project 차단


@pytest.mark.asyncio
async def test_legacy_subscriber_without_project_receives_all():
    """project_id 미지정(레거시) 구독자는 기존대로 전체 수신(하위호환)."""
    hub = WebSocketHub()
    q = await hub.register(None)
    await hub.publish("room-1", {"x": 1}, project_id="ProjA")
    assert q.qsize() == 1


@pytest.mark.asyncio
async def test_room_filter_within_project():
    """project 일치 + room 지정 시 그 room 만 수신."""
    hub = WebSocketHub()
    q = await hub.register({"room-1"}, "ProjA")
    await hub.publish("room-2", {"x": 1}, project_id="ProjA")  # 같은 프로젝트 다른 방
    assert q.qsize() == 0
    await hub.publish("room-1", {"x": 2}, project_id="ProjA")
    assert q.qsize() == 1


@pytest.mark.asyncio
async def test_update_subscription_preserves_project_isolation():
    """구독 rooms 변경(subscribe 메시지)이 격리키를 풀면 안 된다."""
    hub = WebSocketHub()
    q = await hub.register(None, "ProjA")
    await hub.update_subscription(q, {"room-1"})  # project_id 미지정 → 기존 유지
    await hub.publish("room-1", {"x": 1}, project_id="ProjB")  # 타 프로젝트
    assert q.qsize() == 0   # 여전히 격리
    await hub.publish("room-1", {"x": 1}, project_id="ProjA")
    assert q.qsize() == 1


@pytest.mark.asyncio
async def test_publish_without_project_id_not_delivered_to_isolated_subscriber():
    """격리 구독자는 project_id 미상(None) push 를 받지 않는다(보수적 차단)."""
    hub = WebSocketHub()
    q = await hub.register(None, "ProjA")
    await hub.publish("room-1", {"x": 1})  # project_id 미지정
    assert q.qsize() == 0
