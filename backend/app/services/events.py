"""In-process event bus + WebSocket hub (DS-40 §10, DS-60 §8, §9.2).

DB commit 이후 update 를 구독자(WebSocket)에게 push 한다.
단일 process MVP 구조이며, multi-process 확장 시 PostgreSQL LISTEN/NOTIFY 로
대체할 수 있다 (DS-60 §9.2).
"""
from __future__ import annotations

import asyncio
from typing import Any


class WebSocketHub:
    def __init__(self) -> None:
        # 구독자별 큐. value = (구독 room 집합(None=프로젝트 내 전체), project_id(None=레거시 전체))
        # project_id 는 프로젝트 격리 방어키다(A-F1 후속, message-stream 누수 차단):
        # 구독자가 project_id 를 지정하면 그 프로젝트의 push 만 받는다. room 미지정(전역
        # 구독)이어도 타 프로젝트로는 누수되지 않는다.
        self._subscribers: dict[asyncio.Queue, tuple[set[str] | None, str | None]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self, rooms: set[str] | None, project_id: str | None = None
    ) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[q] = (rooms, project_id)
        return q

    async def unregister(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.pop(q, None)

    async def update_subscription(
        self, q: asyncio.Queue, rooms: set[str] | None, project_id: str | None = None
    ) -> None:
        async with self._lock:
            if q in self._subscribers:
                # project_id 미지정 시 기존 격리키를 유지한다(구독 변경이 격리를 풀면 안 됨).
                _, cur_pid = self._subscribers[q]
                self._subscribers[q] = (rooms, project_id if project_id is not None else cur_pid)

    async def publish(
        self, room_id: str, payload: dict[str, Any], project_id: str | None = None
    ) -> None:
        async with self._lock:
            targets = list(self._subscribers.items())
        for q, (rooms, sub_pid) in targets:
            # 프로젝트 격리: 구독자가 project 를 지정했으면 일치할 때만 전달(cross-project 차단).
            if sub_pid is not None and sub_pid != project_id:
                continue
            if rooms is None or room_id in rooms:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # 느린 구독자는 drop. FE 는 polling fallback 으로 복구한다.
                    pass


hub = WebSocketHub()
