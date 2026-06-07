"""Collector 내부 수집 스키마 (DS-40 §15.1, §15.4)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CollectMessageRequest(BaseModel):
    agent_session_id: str
    role_id: str
    surface_id: str
    source: str               # role_log | read_screen
    message_type: str         # log_line | status | error | unmatched
    raw_text: str | None = None
    normalized_text: str
    raw_hash: str | None = None
    correlation_id: str | None = None
    occurred_at: datetime


class CollectEventRequest(BaseModel):
    agent_session_id: str | None = None
    message_id: str | None = None
    correlation_id: str | None = None
    event_type: str
    source: str               # cmux_adapter | role_log_collector | hook | read_screen | backend | artifact_service | postgres_notify
    hook_provider: str | None = None
    hook_event_name: str | None = None
    severity: str = "info"
    payload: dict | None = None
    occurred_at: datetime
