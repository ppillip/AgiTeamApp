"""백그라운드 폴링 루프 (제우스 2026-06-07 / DV-25 정정).

- discovery loop: cmux tree --all 주기 폴링 → 레지스트리 갱신(+liveness).
- transcript loop: **안전망 fallback** (DV-25 정정). 주 경로는 hook_stop 즉시수집
  (collector_service.collect_event → TranscriptCollector.collect_session)이며,
  이 루프는 hook 누락/유실 대비로 길게(transcript_poll_seconds=30s) 보조 tail 한다.
- raw log loop: .agiteam/logs/<role>.log 를 tail → 진단 runtime_event(raw_tui_capture)로 격하.

DB/cmux/파일 일시 장애에도 죽지 않고 다음 주기에 재시도한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..db import repositories as repo
from .cmux_adapter import CmuxAdapter
from .cmux_discovery import DiscoveryRegistry, SurfaceInfo
from .events import hub
from .log_collector import LogCollector
from .transcript_collector import TranscriptCollector, session_registry_singleton


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _upsert_discovered_room(db, info: SurfaceInfo):
    room = await repo.upsert_room(
        db,
        project_id=info.project_id,
        role_id=info.role_id,
        display_name=info.display_name,
        agent_type=info.agent_type,
        team_session_id=info.team_session_id,
        agent_id=info.agent_id,
    )
    room.current_surface_id = info.surface_id
    room.ready_state = "ready" if info.connection_state == "connected" else room.ready_state
    room.updated_at = _now()
    return room


async def _sync_discovery_to_db(registry: DiscoveryRegistry, sessionmaker, changes: list[dict[str, Any]]) -> None:
    """인메모리 디스커버리 진실을 DB 방/런타임 이벤트/WS에 반영한다."""
    async with sessionmaker() as db:
        rooms_by_key = {}
        for info in registry.all_roles():
            room = await _upsert_discovered_room(db, info)
            rooms_by_key[(info.project_id, info.role_id)] = room

        published: list[tuple[str, dict[str, Any]]] = []
        for change in changes:
            key = (change["project_id"], change["role_id"])
            room = rooms_by_key.get(key)
            if room is None:
                info = registry.resolve(change["project_id"], change["role_id"])
                if info is None:
                    continue
                room = await _upsert_discovered_room(db, info)
                rooms_by_key[key] = room
            payload = {
                "project_id": change["project_id"],
                "role_id": change["role_id"],
                "surface_id": change["surface_id"],
                "display_name": change["display_name"],
                "from_state": change["from_state"],
                "to_state": change["to_state"],
                "reason": change["reason"],
            }
            ev = await repo.create_event(
                db,
                room_id=room.room_id,
                event_type="connection_changed",
                source="cmux_discovery",
                severity="info",
                payload_json=payload,
                masked_payload_json=payload,
                occurred_at=change["occurred_at"],
            )
            published.append(
                (
                    str(room.room_id),
                    {
                        "type": "message_update",
                        "cursor": ev.occurred_at.isoformat(),
                        "data": {
                            "update_id": f"event:{ev.event_id}",
                            "room_id": str(room.room_id),
                            "correlation_id": None,
                            "update_type": "room_connection_changed",  # DS-40 update_type 정합 (DV-42)
                            "message": None,
                            "event": {
                                "event_id": str(ev.event_id),
                                "event_type": ev.event_type,
                                "source": ev.source,
                                "severity": ev.severity,
                                "payload": payload,
                                "occurred_at": ev.occurred_at.isoformat(),
                            },
                            "occurred_at": ev.occurred_at.isoformat(),
                        },
                    },
                )
            )
        await db.commit()

    for room_id, payload in published:
        await hub.publish(room_id, payload)


async def discovery_loop(settings: Settings, registry: DiscoveryRegistry, adapter: CmuxAdapter, sessionmaker) -> None:
    while True:
        try:
            tree = await adapter.tree()
            if tree:
                from_snapshot = tree.startswith("# cmux_session_snapshot_fallback")
                metadata = await adapter.runtime_metadata(tree)
                changes = registry.refresh_from_tree(
                    tree,
                    metadata,
                    missed_threshold=settings.discovery_missed_threshold,
                )
                logger.debug(
                    "cmux discovery refreshed projects=%s metadata=%s changes=%s",
                    len(registry.projects()),
                    len(metadata),
                    len(changes),
                )
                if not from_snapshot:
                    for info in registry.connected_roles():
                        if not await adapter.ping(info.surface_id, info.workspace_id, info.tty):
                            change = registry.mark_disconnected(info.project_id, info.role_id)
                            if change:
                                changes.append(change)
                await _sync_discovery_to_db(registry, sessionmaker, changes)
            else:
                logger.warning("cmux discovery tree empty")
        except Exception:
            logger.exception("cmux discovery loop failed")
        await asyncio.sleep(settings.discovery_poll_seconds)


async def transcript_loop(collector: TranscriptCollector, settings: Settings) -> None:
    while True:
        try:
            await collector.collect_once()
        except Exception:
            logger.exception("transcript fallback loop failed")
        await asyncio.sleep(settings.transcript_poll_seconds)


async def rawlog_loop(collector: LogCollector, settings: Settings) -> None:
    while True:
        try:
            await collector.collect_once()
        except Exception:
            logger.exception("raw log loop failed")
        await asyncio.sleep(settings.logtail_poll_seconds)


class BackgroundManager:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    def start(self, settings: Settings, registry: DiscoveryRegistry, sessionmaker) -> None:
        adapter = CmuxAdapter(settings.cmux_bin, settings.cmux_timeout_seconds)
        transcript = TranscriptCollector(settings, registry, sessionmaker, session_registry_singleton)
        rawlog = LogCollector(settings, registry, sessionmaker)
        logger.info("starting background loops cmux_bin=%s", settings.cmux_bin)
        self._tasks.append(asyncio.create_task(discovery_loop(settings, registry, adapter, sessionmaker)))
        self._tasks.append(asyncio.create_task(transcript_loop(transcript, settings)))
        self._tasks.append(asyncio.create_task(rawlog_loop(rawlog, settings)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
