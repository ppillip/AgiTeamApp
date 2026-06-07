"""백그라운드 폴링 루프 (제우스 2026-06-07).

- discovery loop: cmux tree --all 주기 폴링 → 레지스트리 갱신(+liveness).
- logtail loop: .agiteam/logs/<role>.log 주기 tail → DB 저장.

DB/cmux 일시 장애에도 죽지 않고 다음 주기에 재시도한다.
"""
from __future__ import annotations

import asyncio
import contextlib

from ..config import Settings
from .cmux_adapter import CmuxAdapter
from .cmux_discovery import DiscoveryRegistry
from .log_collector import LogCollector


async def discovery_loop(settings: Settings, registry: DiscoveryRegistry, adapter: CmuxAdapter) -> None:
    while True:
        try:
            tree = await adapter.tree()
            if tree:
                registry.refresh_from_tree(tree)
        except Exception:  # noqa: BLE001  (루프 생존 우선)
            pass
        await asyncio.sleep(settings.discovery_poll_seconds)


async def logtail_loop(collector: LogCollector, settings: Settings) -> None:
    while True:
        try:
            await collector.collect_once()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(settings.logtail_poll_seconds)


class BackgroundManager:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    def start(self, settings: Settings, registry: DiscoveryRegistry, sessionmaker) -> None:
        adapter = CmuxAdapter(settings.cmux_bin, settings.cmux_timeout_seconds)
        collector = LogCollector(settings, registry, sessionmaker)
        self._tasks.append(asyncio.create_task(discovery_loop(settings, registry, adapter)))
        self._tasks.append(asyncio.create_task(logtail_loop(collector, settings)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
