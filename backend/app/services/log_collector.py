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
from dataclasses import dataclass
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

# raw role log 는 진단 event 로만 보존 (DS-30 §4.4, runtime_event source/type)
RAW_LOG_SOURCE = "raw_log_collector"
RAW_TUI_EVENT = "raw_tui_capture"

# 에이전트 동작중/조용함 liveness (요구사항 15-1, DS-30 runtime_activity_changed).
# 본문 파싱·작업의미 판정 금지 — 순수 offset 증가 여부만. active=출력 있었음, idle=조용함.
RUNTIME_ACTIVITY_EVENT = "runtime_activity_changed"
ACTIVITY_ACTIVE = "active"
ACTIVITY_IDLE = "idle"
ACTIVITY_UNKNOWN = "unknown"
REASON_OUTPUT = "raw_pty_output"
REASON_QUIET = "raw_pty_quiet"

# event sample 로 보존할 최대 길이(전체 raw stream 미보존 — DS-60 §6.5)
_SAMPLE_MAX = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- 동작중/조용함 상태 (순수 로직, 단위테스트 대상) ----------------------------


@dataclass
class ActivityState:
    """role 단위 liveness 상태. (project_id, role) 키로 보존."""

    activity: str = ACTIVITY_UNKNOWN          # active | idle | unknown
    last_offset: int = 0                       # 마지막으로 관측한 byte offset
    last_active_ts: datetime | None = None     # 마지막 출력(offset 증가) 시각


def decide_activity(
    state: ActivityState,
    had_output: bool,
    now: datetime,
    idle_threshold_seconds: float,
) -> tuple[str, str, float] | None:
    """offset 증가 여부로 다음 활동상태 전환을 판정한다(펄럭임 방지).

    규칙(PM 확정):
    - active 는 즉시 전환: 직전 폴링 대비 offset 증가 1회면 곧장 active.
    - idle 는 active→idle 에만 threshold 적용: 무출력이 idle_threshold_seconds 경과해야 전환.
    - 본문 의미 해석 없음. 출력 있었음/조용함 그 이상 판단하지 않는다.

    반환: (new_activity, reason, idle_for_seconds) 또는 전환 없으면 None.
    """
    if had_output:
        if state.activity != ACTIVITY_ACTIVE:
            # 직전 조용했던 시간(있으면) — active 전환 payload 의 idle_for_seconds 참고값.
            gap = (now - state.last_active_ts).total_seconds() if state.last_active_ts else 0.0
            return (ACTIVITY_ACTIVE, REASON_OUTPUT, max(0.0, gap))
        return None
    # 무출력: active 상태가 threshold 이상 지속되면 idle 로.
    if state.activity == ACTIVITY_ACTIVE and state.last_active_ts is not None:
        idle_for = (now - state.last_active_ts).total_seconds()
        if idle_for >= idle_threshold_seconds:
            return (ACTIVITY_IDLE, REASON_QUIET, idle_for)
    return None


class ActivityRegistry:
    """(project_id, role) → ActivityState 인메모리 저장 (cmux discovery registry 패턴).

    LogCollector 가 갱신하고, REST 방 목록(rooms 라우터)이 현재값을 읽는다(초기 로드/재연결
    시 FE 즉시 반영). 단일 프로세스 MVP — 프로세스 재시작 시 unknown 으로 초기화된다.
    """

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], ActivityState] = {}

    def state(self, project_id: str, role: str) -> ActivityState:
        return self._map.setdefault((project_id, role), ActivityState())

    def get(self, project_id: str, role: str) -> str:
        st = self._map.get((project_id, role))
        return st.activity if st else ACTIVITY_UNKNOWN


# 모듈 싱글톤 — rooms 라우터가 읽는다.
activity_registry = ActivityRegistry()


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
        activity: ActivityRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.sessionmaker = sessionmaker
        # 파일 경로 → 마지막으로 읽은 byte offset
        self._offsets: dict[str, int] = {}
        # (project_id, role) → 동작중/조용함 상태. 기본은 모듈 싱글톤(REST 가 읽는 것과 공유).
        self.activity = activity or activity_registry

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

    async def collect_once(self, now: datetime | None = None) -> int:
        """1회 폴링. 저장한 event 수(진단 + 활동전환) 반환.

        무출력 role 도 매 폴링 평가해야 active→idle 전환을 잡으므로, chunk 유무와 무관하게
        모든 role 에 대해 활동상태를 판정한다.
        """
        ts = now or _now()
        saved = 0
        for proj in self.registry.projects():
            project_id = proj["project_id"]
            for role, path in self._iter_log_files(project_id):
                chunk, start = self._read_new(path)
                # 활동상태 판정·전환 (offset 증가 여부만 사용, 본문 해석 없음)
                saved += await self._update_activity(project_id, role, path, chunk, start, ts)
                # 기존 진단 격하 보존(raw_tui_capture) — chunk 있을 때만
                if chunk:
                    saved += await self._store_diagnostic(project_id, role, path, chunk, start)
        return saved

    async def _update_activity(
        self, project_id: str, role: str, path: Path, chunk: str, start: int, now: datetime
    ) -> int:
        """role.log offset 증가/무출력으로 active↔idle 전환 판정. 전환 시에만 event/publish.

        반환: 전환을 발행했으면 1, 아니면 0.
        """
        st = self.activity.state(project_id, role)
        had_output = bool(chunk)
        chunk_bytes = len(chunk.encode("utf-8", "replace")) if had_output else 0

        decision = decide_activity(st, had_output, now, self.settings.activity_idle_seconds)

        # 상태 갱신: 출력이 있었으면 항상 last_active_ts/offset 전진(전환 여부와 무관).
        if had_output:
            st.last_active_ts = now
            st.last_offset = start + chunk_bytes

        if decision is None:
            return 0

        new_activity, reason, idle_for = decision
        from_activity = st.activity
        st.activity = new_activity

        if new_activity == ACTIVITY_ACTIVE:
            offset_start, offset_end = start, start + chunk_bytes
        else:  # idle: 신규 출력 없음 — 마지막 관측 offset 고정
            offset_start = offset_end = st.last_offset

        payload = {
            "project_id": project_id,
            "role": role,
            "runtime_activity": new_activity,
            "from_activity": from_activity,
            "ts": now.isoformat(),
            "reason": reason,
            "offset_start": offset_start,
            "offset_end": offset_end,
            "chunk_bytes": chunk_bytes,
            "idle_for_seconds": round(idle_for, 3),
            "idle_threshold_seconds": self.settings.activity_idle_seconds,
        }
        return await self._emit_activity_change(project_id, role, payload, now)

    async def _emit_activity_change(
        self, project_id: str, role: str, payload: dict, now: datetime
    ) -> int:
        """활동전환을 runtime_event 로 저장 + 기존 message-stream WS 로 push.

        WS 계약은 connection_changed 와 동일 봉투(update_type 만 runtime_activity_changed).
        DB/세션 일시장애에도 죽지 않는다(다음 폴링 재평가). payload 는 offset/enum 뿐이라
        민감정보 없음 → masked_payload 동일.
        """
        info = self.registry.resolve(project_id, role)
        display_name = info.display_name if info else role
        surface_id = info.surface_id if info else None
        try:
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
                ev = await repo.create_event(
                    db,
                    room_id=room.room_id,
                    event_type=RUNTIME_ACTIVITY_EVENT,
                    source=RAW_LOG_SOURCE,
                    severity="info",
                    payload_json=payload,
                    masked_payload_json=payload,
                    occurred_at=now,
                )
                room_id = str(room.room_id)
                event_id = str(ev.event_id)
                await db.commit()
        except Exception:  # noqa: BLE001  (DB 장애가 watcher/루프를 죽이지 않음)
            return 0

        ws_payload = {
            "type": "message_update",
            "cursor": now.isoformat(),
            "data": {
                "update_id": f"event:{event_id}",
                "room_id": room_id,
                "correlation_id": None,
                "update_type": RUNTIME_ACTIVITY_EVENT,
                "message": None,
                "event": {
                    "event_id": event_id,
                    "event_type": RUNTIME_ACTIVITY_EVENT,
                    "source": RAW_LOG_SOURCE,
                    "severity": "info",
                    "payload": payload,
                    "occurred_at": now.isoformat(),
                },
                "occurred_at": now.isoformat(),
            },
        }
        await hub.publish(room_id, ws_payload, project_id)
        return 1

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
