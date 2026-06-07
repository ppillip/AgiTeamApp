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
        # 구독자별 큐. value = 구독 room 집합(None=전체)
        self._subscribers: dict[asyncio.Queue, set[str] | None] = {}
        self._lock = asyncio.Lock()

    async def register(self, rooms: set[str] | None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[q] = rooms
        return q

    async def unregister(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.pop(q, None)

    async def update_subscription(self, q: asyncio.Queue, rooms: set[str] | None) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers[q] = rooms

    async def publish(self, room_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subscribers.items())
        for q, rooms in targets:
            if rooms is None or room_id in rooms:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # 느린 구독자는 drop. FE 는 polling fallback 으로 복구한다.
                    pass


hub = WebSocketHub()
