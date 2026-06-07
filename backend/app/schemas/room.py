"""방 요약 스키마 (DS-40 §4.4)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LastMessage(BaseModel):
    message_id: str
    text: str | None = None
    direction: str
    status: str
    occurred_at: datetime


class RoomSummary(BaseModel):
    room_id: str
    project_id: str
    role: str                 # 공개 응답 필드 (QI-WG-009)
    display_name: str
    agent_type: str | None = None
    room_type: str
    surface_id: str | None = None
    agent_session_id: str | None = None
    ready_state: str
    collector_state: str
    last_message: LastMessage | None = None
    last_message_at: datetime | None = None
    read_marker_at: datetime | None = None
    unread_count: int = 0
