"""메시지/런타임 스키마 (DS-40 §4.1~4.3, §7)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RuntimeContext(BaseModel):
    room_id: str
    role: str                 # 공개 응답 필드 (QI-WG-009). DB role_id ← 직렬화기에서 매핑
    display_name: str
    agent_type: str | None = None
    surface_id: str | None = None
    agent_session_id: str | None = None
    ready_state: str
    collector_state: str


class Message(BaseModel):
    message_id: str
    room_id: str
    correlation_id: str | None = None
    role: str                 # 공개 응답 필드 (QI-WG-009)
    surface_id: str | None = None
    agent_session_id: str | None = None
    direction: str
    source: str
    message_type: str
    text: str | None = None
    status: str
    occurred_at: datetime
    recorded_at: datetime
    updated_at: datetime | None = None


class MessageUpdate(BaseModel):
    update_id: str
    room_id: str
    correlation_id: str | None = None
    update_type: str
    message: Message | None = None
    event: dict | None = None
    occurred_at: datetime


class AttachmentRef(BaseModel):
    # WG-MSG-06 으로 사전 업로드한 첨부 참조 (DS-40 §7.5)
    attachment_id: str


class SendMessageRequest(BaseModel):
    # [라우팅 확정] 송신 대상은 항상 PM. project_id 로 어느 팀의 PM 인지 지정한다.
    # room_id/role_id 는 DS-40 호환을 위해 선택 수용하되 라우팅에 사용하지 않는다.
    # text 는 attachments 가 있으면 빈 문자열 허용(DS-40 §7.5) → 기본값 "".
    text: str = ""
    project_id: str | None = None
    room_id: str | None = None
    role_id: str | None = None
    client_message_id: str | None = None
    attachments: list[AttachmentRef] | None = None


class SendAck(BaseModel):
    accepted: bool
    send_submitted: bool
    message_id: str
    correlation_id: str
    room_id: str
    role: str                 # 공개 응답 필드 (QI-WG-009)
    surface_id: str | None = None
    agent_session_id: str | None = None
    status: str
    client_message_id: str | None = None
    submitted_at: datetime


class ReadRequest(BaseModel):
    read_until: datetime | None = None
    last_read_message_id: str | None = None
