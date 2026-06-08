"""Transcript JSONL canonical collector (DV-25, DS-60 §6.3 / §10.2).

대화 본문 canonical 의 단일 출처(transcript JSONL)를 offset 기반으로 tail 해
``webgui_message`` 로 정규화 저장한다. 기존 tee raw stdout 본문 파서를 대체한다.

수집 단위는 'transcript session' 이다. (provider, session_id) → (project_id, role) 매핑은
hook 이벤트(SessionStart/UserPromptSubmit 의 session_id/transcript_path/cwd)로 등록되며
(``register_session``), hook 보강 전에는 transcript 파일 탐색 후보로 보조 해소한다.

저장 규칙(DS-60 §6.3 / §6.8):
- assistant record → direction=inbound, source=transcript, message_type=assistant_message.
  방의 open outbound correlation 에 매칭, 없으면 status=unmatched.
- user record → direction=outbound, source=transcript, message_type=user_message.
  PM Bridge 선저장본과 중복되면 bridge 를 canonical 로 유지하고 transcript 측은 skip.
- 중복 방지: (provider, transcript_record_id) unique. record_id 부재 시 (room,source,raw_hash).

DB/파일 일시 장애에도 죽지 않고 다음 폴링에서 재시도한다.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..db import repositories as repo
from .cmux_discovery import DiscoveryRegistry
from .events import hub
from .masking import mask_text
from .transcript_parser import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    KIND_ASSISTANT,
    TranscriptRecord,
    claude_cwd_slug,
    codex_cwd_of,
    parse_records,
)

TRANSCRIPT_SOURCE = "transcript"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claude_root() -> Path:
    return Path.home() / ".claude" / "projects"


def codex_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def find_claude_files(project_root: str | Path, session_id: str | None = None) -> list[Path]:
    """Claude transcript 후보 파일. session_id 지정 시 해당 파일만, 없으면 최신순 전체."""
    slug_dir = claude_root() / claude_cwd_slug(project_root)
    if not slug_dir.is_dir():
        return []
    if session_id:
        f = slug_dir / f"{session_id}.jsonl"
        return [f] if f.is_file() else []
    return sorted(slug_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def find_codex_files(project_root: str | Path, session_id: str | None = None) -> list[Path]:
    """Codex rollout 후보 파일. session_id 매칭 우선, 없으면 cwd 일치 최신순."""
    root = codex_root()
    if not root.is_dir():
        return []
    candidates = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    target = str(Path(project_root).resolve())
    out: list[Path] = []
    for path in candidates:
        if session_id and session_id in path.name:
            out.append(path)
            continue
        if session_id:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if codex_cwd_of(text) == target:
            out.append(path)
    return out


@dataclass
class TranscriptSession:
    provider: str
    session_id: str
    project_id: str
    role: str
    agent_id: str | None = None    # 방 라우팅 1차 키 (1에이전트=1방, 유저 확정 2026-06-08)
    room_id: str | None = None      # hook 이 특정한 방. _store_record 는 이 방에만 저장
    file_path: Path | None = None
    offset: int = 0
    started: bool = False  # 최초 발견 시 EOF 부터 시작했는지


class TranscriptSessionRegistry:
    """에이전트 transcript 세션 인메모리 레지스트리.

    라우팅 절대 원칙(유저 확정 2026-06-08): **1에이전트=1방, 키=AGENT_ID**.
    여러 Claude/여러 Codex 를 동시 구동하므로 provider/cwd 로 뭉뚱그리지 않는다.
    AGENT_ID 가 있으면 그것을 1차 키로, 없으면 (provider, session_id) 로 보조 식별한다.

    hook 이벤트가 session 을 등록하는 canonical 경로다(DS-60 §6.3/§7).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, TranscriptSession] = {}

    @staticmethod
    def _key(agent_id: str | None, provider: str | None, session_id: str | None) -> str:
        if agent_id:
            return f"agent:{agent_id}"
        return f"sid:{provider}:{session_id}"

    def register(
        self,
        provider: str,
        session_id: str,
        project_id: str,
        role: str,
        transcript_path: str | None = None,
        *,
        agent_id: str | None = None,
        room_id: str | None = None,
    ) -> None:
        if not provider or not session_id:
            return
        key = self._key(agent_id, provider, session_id)
        with self._lock:
            sess = self._sessions.get(key)
            # agent_id 보강 전 (provider, session_id) 로 먼저 등록됐던 세션을 agent 키로 승격/병합
            if sess is None and agent_id:
                legacy_key = self._key(None, provider, session_id)
                legacy = self._sessions.pop(legacy_key, None)
                if legacy is not None:
                    sess = legacy
                    self._sessions[key] = sess
            if sess is None:
                sess = TranscriptSession(provider, session_id, project_id, role)
                self._sessions[key] = sess
            sess.project_id = project_id
            sess.role = role
            if agent_id:
                sess.agent_id = agent_id
            if room_id:
                sess.room_id = room_id
            # session_id 갱신: 재부팅 등으로 같은 agent 가 새 세션을 시작하면 최신 session_id 반영
            if session_id and sess.session_id != session_id:
                sess.session_id = session_id
            if transcript_path:
                p = Path(transcript_path)
                if p.is_file():
                    # 파일이 바뀌면(재부팅 새 transcript) offset 리셋 → 새 파일을 처음부터 수집
                    if sess.file_path is not None and sess.file_path != p:
                        sess.offset = 0
                        sess.started = False
                    sess.file_path = p

    def sessions(self) -> list[TranscriptSession]:
        with self._lock:
            return list(self._sessions.values())

    def get(self, provider: str, session_id: str) -> TranscriptSession | None:
        """(provider, session_id) 로 세션 조회 (agent_id 로 승격된 세션 포함 탐색)."""
        with self._lock:
            direct = self._sessions.get(self._key(None, provider, session_id))
            if direct is not None:
                return direct
            for s in self._sessions.values():
                if s.provider == provider and s.session_id == session_id:
                    return s
            return None

    def get_by_agent(self, agent_id: str) -> TranscriptSession | None:
        """AGENT_ID 1차 키로 세션 조회 (라우팅 정본)."""
        if not agent_id:
            return None
        with self._lock:
            direct = self._sessions.get(self._key(agent_id, None, None))
            if direct is not None:
                return direct
            for s in self._sessions.values():
                if s.agent_id == agent_id:
                    return s
            return None


class TranscriptCollector:
    def __init__(
        self,
        settings: Settings,
        registry: DiscoveryRegistry,
        sessionmaker: async_sessionmaker,
        session_registry: TranscriptSessionRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.sessionmaker = sessionmaker
        self.sessions = session_registry or session_registry_singleton

    # --- 파일 해소/tail -----------------------------------------------------

    def _resolve_file(self, sess: TranscriptSession) -> Path | None:
        if sess.file_path and sess.file_path.is_file():
            return sess.file_path
        root = self.settings.project_root(sess.project_id)
        if sess.provider == PROVIDER_CLAUDE:
            files = find_claude_files(root, sess.session_id)
        elif sess.provider == PROVIDER_CODEX:
            files = find_codex_files(root, sess.session_id)
        else:
            files = []
        if files:
            sess.file_path = files[0]
            return sess.file_path
        return None

    def _read_new(self, sess: TranscriptSession, path: Path, *, skip_history: bool = True) -> str:
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if not sess.started:
            sess.started = True
            if skip_history:
                # 폴링 최초 발견: 히스토리 폭주 방지를 위해 EOF 부터 시작
                sess.offset = size
                return ""
            # hook 트리거 최초 수집: 현재 offset(보통 0) 부터 EOF 까지 그대로 적재
        if size < sess.offset:  # rotation/truncate
            sess.offset = 0
        if size == sess.offset:
            return ""
        try:
            with open(path, "rb") as f:
                f.seek(sess.offset)
                data = f.read()
        except OSError:
            return ""
        sess.offset += len(data)
        return data.decode("utf-8", "replace")

    # --- 수집 (폴링 fallback + hook 트리거 즉시수집 공용) ----------------------

    async def _collect_for_session(self, sess: TranscriptSession, *, skip_history: bool) -> int:
        """단일 세션 transcript 를 offset 이후로 tail 해 신규 record 를 저장한다.

        에이전트 격리: file_path(hook 이 전달한 격리 transcript) 가 있으면 그 파일만 읽고,
        없을 때만 session_id 기준으로 해소한다(같은 cwd-slug 혼재를 가정하지 않음).
        """
        path = self._resolve_file(sess)
        if path is None:
            return 0
        chunk = self._read_new(sess, path, skip_history=skip_history)
        if not chunk:
            return 0
        saved = 0
        for rec in parse_records(sess.provider, chunk):
            saved += await self._store_record(sess, path, rec)
        return saved

    async def collect_once(self) -> int:
        """폴링 fallback (DV-25). hook 트리거가 주 경로이며 이 루프는 안전망이다."""
        saved = 0
        for sess in self.sessions.sessions():
            info = self.registry.resolve(sess.project_id, sess.role)
            if info is not None and info.connection_state != "connected":
                continue
            saved += await self._collect_for_session(sess, skip_history=True)
        return saved

    async def collect_session(
        self,
        provider: str | None = None,
        session_id: str | None = None,
        *,
        agent_id: str | None = None,
    ) -> int:
        """hook 트리거: 특정 에이전트 세션의 transcript 를 즉시 1회 수집한다.

        AGENT_ID 1차 키로 세션을 특정(1에이전트=1방), 없으면 (provider, session_id) 보조.
        폴링과 달리 EOF 스킵 없이 현재 offset 이후를 적재하고 cmux 연결상태도 따지지 않는다
        (hook 이 떴다 = 그 에이전트가 활성).
        """
        sess = None
        if agent_id:
            sess = self.sessions.get_by_agent(agent_id)
        if sess is None and provider and session_id:
            sess = self.sessions.get(provider, session_id)
        if sess is None:
            return 0
        return await self._collect_for_session(sess, skip_history=False)

    # --- 저장 ---------------------------------------------------------------

    async def _store_record(self, sess: TranscriptSession, path: Path, rec: TranscriptRecord) -> int:
        text = (rec.text or "").strip()
        if not text:
            return 0
        is_assistant = rec.kind == KIND_ASSISTANT
        direction = "inbound" if is_assistant else "outbound"
        record_id = rec.record_id
        raw_hash = "sha256:" + hashlib.sha256(
            f"{sess.provider}|{record_id or ''}|{sess.role}|{text}".encode()
        ).hexdigest()
        transcript_path_masked = mask_text(f"{sess.provider}:{path.name}")
        occurred = rec.occurred_at or _now()

        async with self.sessionmaker() as db:
            # 방 라우팅(유저 확정 2026-06-08): hook 이 특정한 room_id 에만 저장한다.
            # provider/role 로 재유도하지 않는다 — 여러 동종 CLI 가 한 방에 합쳐지면 안 됨.
            room = None
            if sess.room_id:
                room = await repo.get_room(db, sess.room_id)
            if room is None:
                room = await repo.upsert_room(
                    db,
                    project_id=sess.project_id,
                    role_id=sess.role,
                    display_name=(self.registry.resolve(sess.project_id, sess.role).display_name
                                  if self.registry.resolve(sess.project_id, sess.role) else sess.role),
                    room_type="pm" if sess.role == "PM" else "role",
                )

            # 1) transcript record 중복 방지
            if record_id is not None:
                dup = await repo.find_message_by_record(db, sess.provider, record_id)
                if dup is not None:
                    return 0
            else:
                dup = await repo.find_message_by_hash(db, room.room_id, TRANSCRIPT_SOURCE, raw_hash)
                if dup is not None:
                    return 0

            # 2) user record = 발신 프롬프트. PM Bridge 선저장본과 중복이면 bridge canonical 유지(skip).
            if not is_assistant:
                bridge_dup = await _find_outbound_text_dup(db, room.room_id, text)
                if bridge_dup is not None:
                    return 0

            # 3) correlation: assistant 는 open outbound 에 매칭, 없으면 unmatched
            correlation_id = None
            status = "received" if is_assistant else "sent"
            message_type = rec.kind
            if is_assistant:
                open_ob = await repo.find_open_outbound(db, room.room_id)
                if open_ob is not None:
                    correlation_id = open_ob.correlation_id
                else:
                    status = "unmatched"
                    message_type = "unmatched"

            msg = await repo.create_message(
                db,
                room_id=room.room_id,
                correlation_id=correlation_id,
                role_id=sess.role,
                surface_id=(self.registry.resolve(sess.project_id, sess.role).surface_id
                            if self.registry.resolve(sess.project_id, sess.role) else None),
                team_session_id=room.team_session_id,   # provenance: FE 세션 구분선 (DV-41)
                direction=direction,
                source=TRANSCRIPT_SOURCE,
                message_type=message_type,
                provider=sess.provider,
                transcript_path=transcript_path_masked,
                transcript_offset=str(sess.offset),
                transcript_record_id=record_id,
                raw_text=mask_text(text),
                normalized_text=text,
                raw_hash=raw_hash,
                status=status,
                occurred_at=occurred,
            )
            await repo.touch_room_last_message(db, room, msg, inbound=is_assistant)
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
                        "update_type": "message_received" if is_assistant else "message_sent",
                        "message": {"message_id": str(msg.message_id), "text": text, "status": status},
                        "event": None,
                        "occurred_at": occurred.isoformat(),
                    },
                },
            )
        return 1


# 유저→PM 선저장(bridge) 출처. transcript 의 같은 메시지를 cross-source 로 매칭할 때
# 이 출처들만 canonical 로 인정한다 (transcript↔transcript 오매칭 방지).
_BRIDGE_SOURCES = ("webgui", "pm_bridge", "bridge")

# cross-source dedup 용 정규화: 표시값(normalized_text, 멀티라인 보존)은 건드리지 않고
# '매칭 키'로만 쓴다. cmux 래핑/개행/연속공백 차이를 흡수하기 위해 모든 공백 run 을
# 단일 스페이스로 접고 trim 한다. (결함수정 2026-06-09: SENT/LIVE TRANSCRIPT 이중 표시)
_WS_RE = re.compile(r"\s+")


def canonical_match_text(s: str | None) -> str:
    return _WS_RE.sub(" ", (s or "").strip())


async def _find_outbound_text_dup(db, room_id, text: str):
    """같은 방의 최근 bridge outbound 중 canonical 텍스트가 일치하는 선저장본을 반환.

    cmux 래핑/공백 차이로 정확 일치(``==``)가 깨지던 문제를 해결하기 위해, 후보를
    최근순으로 받아 Python 에서 canonical 비교한다. 일치 시 bridge 가 canonical 이므로
    transcript insert 를 skip 한다.
    """
    from sqlalchemy import select

    from ..db.models import WebguiMessage

    target = canonical_match_text(text)
    if not target:
        return None

    res = await db.execute(
        select(WebguiMessage)
        .where(
            WebguiMessage.room_id == room_id,
            WebguiMessage.direction == "outbound",
            WebguiMessage.source.in_(_BRIDGE_SOURCES),
        )
        .order_by(WebguiMessage.recorded_at.desc())
        .limit(50)
    )
    for cand in res.scalars().all():
        if canonical_match_text(cand.normalized_text) == target:
            return cand
    return None


# 앱 전역 singleton (hook 이 등록, collector 가 소비)
session_registry_singleton = TranscriptSessionRegistry()
