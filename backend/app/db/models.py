"""ORM 모델 (DS-30 §4 테이블 정의 기준, PostgreSQL).

타입은 DS-30 물리 타입과 1:1 대응:
- PK/FK: uuid
- 시간: timestamptz
- 구조화 payload: jsonb
허용값은 PostgreSQL CHECK 제약(=DDL) 으로 강제하며, ORM 은 동일 문자열을 사용한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


TS = TIMESTAMP(timezone=True)


class WebguiRoom(Base):
    __tablename__ = "webgui_room"

    room_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(Text, nullable=False)
    role_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 방 canonical 안정키 (DS-40/60 QI-WG-022 정합): project_id + role_id.
    # team_session_id / agent_id 는 방 식별키가 아니라 현재 실행 세션·provenance 검증값이다.
    team_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    room_type: Mapped[str] = mapped_column(Text, nullable=False, default="role")
    current_surface_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_agent_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ready_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    last_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    read_marker_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())

    __table_args__ = (
        # canonical 유일성: (project_id, role_id) — 1 프로젝트 1 역할 1 방.
        # 재부팅(team_session_id 변경)·agent_id 변화로 방을 증식시키지 않는다 (QI-WG-022).
        Index(
            "uk_webgui_room_project_role",
            "project_id", "role_id",
            unique=True,
        ),
    )


class WebguiAgentSession(Base):
    __tablename__ = "webgui_agent_session"

    agent_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webgui_room.room_id"), nullable=False
    )
    role_id: Mapped[str] = mapped_column(Text, nullable=False)
    surface_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    ready_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    collector_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class WebguiMessage(Base):
    __tablename__ = "webgui_message"

    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webgui_room.room_id"), nullable=False
    )
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webgui_agent_session.agent_session_id"), nullable=True
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    role_id: Mapped[str] = mapped_column(Text, nullable=False)
    surface_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(Text, nullable=False, default="user_message")
    # provenance: 메시지가 속한 팀 부팅 세션(현재값 아님, 생성 시점 고정). FE 세션 구분선용 (DV-41).
    team_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # transcript canonical 추적 필드 (DV-25, DS-30 §4.3)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_offset: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 이미지 첨부 공개 메타 목록 (DV-90, DS-40 §4.2.1). host 절대경로는 담지 않는다.
    attachments_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    occurred_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    recorded_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class WebguiRuntimeEvent(Base):
    __tablename__ = "webgui_runtime_event"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webgui_room.room_id"), nullable=False
    )
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webgui_agent_session.agent_session_id"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webgui_message.message_id"), nullable=True
    )
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    hook_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_event_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    masked_payload_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    recorded_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
