"""Raw role log 진단 수집 (DV-25 재작성, DS-60 §6.5 / §10.2).

[수집 방향 확정 — 제우스/아테나 2026-06-08]
기존 tee raw stdout '본문' 파서는 폐기한다. ``<project>/.agiteam/logs/<role>.log`` 는
대화가 아니라 TUI 화면 그리기 ANSI repaint stream(cursor 이동/alternate screen)이므로,
WebGUI 말풍선 '본문'(webgui_message)으로 저장하지 않는다.

본 collector 는 raw role log append 를 tail 해 **진단 보조 event** 로만 보존한다:
- 저장 대상: ``webgui_runtime_event`` (event_type=raw_tui_capture, source=raw_log_collector)
- 저장 내용: chunk 크기/offset/raw hash/ANSI 포함 여부/마스킹된 sample 요약 (전체 raw stream 미보존)
- 말풍선 본문 canonical 은 transcript_collector(transcript JSONL) 가 담당한다.

식별/저장 키는 (project_id, role). offset 으로 신규 append 만 읽어 중복을 피한다.
DB/cmux 일시 장애에도 죽지 않고 다음 폴링에서 재시도한다.
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
from .masking import mask_text

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CANON_ROLES = {"PM", "Architect", "DeveloperBE", "DeveloperFE", "Designer", "QA", "DevOps"}

# raw role log 는 진단 event 로만 보존 (DS-30 §4.4, runtime_event source/type)
RAW_LOG_SOURCE = "raw_log_collector"
RAW_TUI_EVENT = "raw_tui_capture"

# event sample 로 보존할 최대 길이(전체 raw stream 미보존 — DS-60 §6.5)
_SAMPLE_MAX = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def has_ansi(text: str) -> bool:
    return bool(_ANSI_RE.search(text))


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

    def _read_new(self, path: Path) -> tuple[str, int]:
        """신규 append 청크와 그 시작 offset 을 반환. 신규 없음이면 ("", offset)."""
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return "", self._offsets.get(key, 0)
        offset = self._offsets.get(key)
        if offset is None:
            # 최초 발견 파일은 히스토리 재적재를 피하기 위해 현재 EOF 부터 시작
            self._offsets[key] = size
            return "", size
        if size < offset:
            # 로그 회전/truncate 감지 → 처음부터
            offset = 0
        if size == offset:
            return "", offset
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return "", offset
        start = offset
        self._offsets[key] = offset + len(data)
        return data.decode("utf-8", "replace"), start

    async def collect_once(self) -> int:
        """1회 폴링. 저장한 진단 event 수 반환."""
        saved = 0
        for proj in self.registry.projects():
            project_id = proj["project_id"]
            for role, path in self._iter_log_files(project_id):
                chunk, start = self._read_new(path)
                if not chunk:
                    continue
                saved += await self._store_diagnostic(project_id, role, path, chunk, start)
        return saved

    async def _store_diagnostic(
        self, project_id: str, role: str, path: Path, chunk: str, start: int
    ) -> int:
        """raw TUI capture 요약을 진단 runtime_event 로 보존. 말풍선 본문 미저장."""
        info = self.registry.resolve(project_id, role)
        surface_id = info.surface_id if info else None
        display_name = info.display_name if info else role

        stripped = strip_ansi(chunk).strip()
        raw_bytes = chunk.encode("utf-8", "replace")
        raw_hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        # 전체 raw stream 미보존 — ANSI 제거 sample 일부만 마스킹해 진단용으로 남긴다.
        sample = mask_text(stripped[:_SAMPLE_MAX]) if stripped else None
        payload = {
            "log_path": f"{self.settings.agiteam_logs_subdir}/{path.name}",
            "chunk_bytes": len(raw_bytes),
            "offset_start": start,
            "offset_end": start + len(raw_bytes),
            "raw_hash": raw_hash,
            "has_ansi": has_ansi(chunk),
            "sample": sample,
        }

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
            await repo.create_event(
                db,
                room_id=room.room_id,
                event_type=RAW_TUI_EVENT,
                source=RAW_LOG_SOURCE,
                severity="debug",
                payload_json=payload,
                masked_payload_json=payload,
                occurred_at=_now(),
            )
            await db.commit()
        return 1
