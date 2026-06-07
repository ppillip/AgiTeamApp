"""로그파일 tail 수집 (제우스 2026-06-07 확정 — 화면긁기/stdout 직접수집 폐기).

<project_root>/.agiteam/logs/<role>.log 를 tail 해 방별(=(project_id, role))로 DB 저장한다.
(agiteam.sh 가 각 CLI 출력을 이 로그로 tee 한다 — 아틀라스 작업)

- 식별/저장 키는 (project_id, role). surface_id 는 디스커버리에서 일시 해소.
- 파일 offset 을 추적해 새로 추가된 내용만 inbound 메시지로 적재(중복 방지).
- DB 미가동 시 graceful: 예외를 흡수하고 다음 폴링에서 재시도.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..db import repositories as repo
from .cmux_discovery import ROLE_TOKEN_MAP, DiscoveryRegistry
from .events import hub
from .masking import mask_text

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CANON_ROLES = {"PM", "Architect", "DeveloperBE", "DeveloperFE", "Designer", "QA", "DevOps"}

# canonical 저장 분류 (QI-WG-006 확정): 로그파일 tail 본문은 source=role_log, message_type=log_line
ROLE_LOG_SOURCE = "role_log"
LOG_LINE_TYPE = "log_line"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def role_from_filename(stem: str) -> str | None:
    """로그 파일명 stem → 정규 role_id. 'PM'/'DeveloperBE'/'be' 등 모두 허용."""
    if stem in _CANON_ROLES:
        return stem
    return ROLE_TOKEN_MAP.get(stem.strip().lower())


class LogCollector:
    def __init__(
        self,
        settings: Settings,
        registry: DiscoveryRegistry,
        sessionmaker: async_sessionmaker,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.sessionmaker = sessionmaker
        # 파일 경로 → 마지막으로 읽은 byte offset
        self._offsets: dict[str, int] = {}

    def _iter_log_files(self, project_id: str):
        logs_dir = self.settings.logs_dir(project_id)
        if not logs_dir.is_dir():
            return
        for entry in sorted(logs_dir.glob("*.log")):
            role = role_from_filename(entry.stem)
            if role:
                yield role, entry

    def _read_new(self, path: Path) -> str:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        offset = self._offsets.get(key)
        if offset is None:
            # 최초 발견 파일은 히스토리 재적재를 피하기 위해 현재 EOF 부터 시작
            self._offsets[key] = size
            return ""
        if size < offset:
            # 로그 회전/truncate 감지 → 처음부터
            offset = 0
        if size == offset:
            return ""
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return ""
        self._offsets[key] = offset + len(data)
        return data.decode("utf-8", "replace")

    async def collect_once(self) -> int:
        """1회 폴링. 저장한 메시지 수 반환."""
        saved = 0
        for proj in self.registry.projects():
            project_id = proj["project_id"]
            for role, path in self._iter_log_files(project_id):
                chunk = self._read_new(path)
                if not chunk.strip():
                    continue
                text = strip_ansi(chunk).strip()
                if not text:
                    continue
                saved += await self._store(project_id, role, text)
        return saved

    async def _store(self, project_id: str, role: str, text: str) -> int:
        info = self.registry.resolve(project_id, role)
        surface_id = info.surface_id if info else None
        display_name = info.display_name if info else role
        raw_hash = "sha256:" + hashlib.sha256(f"{project_id}|{role}|{text}".encode()).hexdigest()
        async with self.sessionmaker() as db:
            room = await repo.upsert_room(
                db,
                project_id=project_id,
                role_id=role,
                display_name=display_name,
                room_type="pm" if role == "PM" else "role",
            )
            if surface_id:
                room.current_surface_id = surface_id
            correlation_id = None
            open_ob = await repo.find_open_outbound(db, room.room_id)
            if open_ob is not None:
                correlation_id = open_ob.correlation_id
            msg = await repo.create_message(
                db,
                room_id=room.room_id,
                correlation_id=correlation_id,
                role_id=role,
                surface_id=surface_id,
                direction="inbound",
                source=ROLE_LOG_SOURCE,
                message_type=LOG_LINE_TYPE,
                raw_text=mask_text(text),
                normalized_text=text,
                raw_hash=raw_hash,
                status="received",
                occurred_at=_now(),
            )
            await repo.touch_room_last_message(db, room, msg, inbound=True)
            await db.commit()
            await hub.publish(
                str(room.room_id),
                {
                    "type": "message_update",
                    "cursor": f"{msg.recorded_at.isoformat()}|message:{msg.message_id}",
                    "data": {
                        "update_id": f"message:{msg.message_id}",
                        "room_id": str(room.room_id),
                        "correlation_id": str(correlation_id) if correlation_id else None,
                        "update_type": "message_received",
                        "message": {"message_id": str(msg.message_id), "text": text, "status": "received"},
                        "event": None,
                        "occurred_at": msg.occurred_at.isoformat(),
                    },
                },
            )
        return 1
