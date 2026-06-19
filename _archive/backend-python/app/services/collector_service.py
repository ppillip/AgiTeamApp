"""Collector 수집 서비스 (DV-20.1 + DV-25 정정).

설계: DS-40 §15, DS-60 §6.7 / §7.
- transcript JSONL parser 가 수집한 inbound/outbound 본문(source=transcript)을 message 로 저장.
- hook 이벤트를 hook_normalizer 로 정규화해 runtime_event 로 저장하고, transcript session 을 등록.
- raw role log/read-screen 본문은 message 가 아니라 runtime_event 로 분리(log_collector 격하).
- correlation 매칭: open outbound 가 있으면 연결, 없으면 unmatched 보존.

WG-CHAT-05(collect_message)/WG-CHAT-06(collect_event) 내부 API 구현이다.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from .. import errors
from ..db import repositories as repo
from ..db.serializers import event_to_dict, message_to_dict
from . import hook_normalizer
from .events import hub
from .masking import mask_payload, mask_text
from .sanitizer import sanitize_tool_leak
from .transcript_collector import TranscriptCollector, session_registry_singleton

# message body canonical source (DV-25)
_MESSAGE_SOURCES = {"bridge", "hook", "transcript", "webgui", "pm_bridge"}
_INBOUND_TYPES = {"assistant_message"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# cli 별칭 → transcript provider 정규화 (DS-60 §7.4)
_CLI_PROVIDER_MAP = {
    "claude": "claude_code",
    "claude_code": "claude_code",
    "claude-code": "claude_code",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "antigravity",
}


def _normalize_provider(cli: str | None) -> str:
    if not cli:
        return "claude_code"
    return _CLI_PROVIDER_MAP.get(cli.strip().lower(), cli.strip().lower())


def _build_default_collector() -> TranscriptCollector:
    """앱 런타임용 기본 TranscriptCollector (전역 session_registry_singleton 공유).

    offset/세션 상태는 singleton 레지스트리에 있으므로 매 호출 새로 만들어도
    백그라운드 폴링 collector 와 동일 상태를 공유한다.
    """
    from ..config import get_settings
    from ..db.base import get_sessionmaker
    from .cmux_discovery import registry as discovery_registry

    return TranscriptCollector(
        get_settings(), discovery_registry, get_sessionmaker(), session_registry_singleton
    )


async def collect_message(db: AsyncSession, room_id: str, body) -> dict:
    """WG-CHAT-05: collector 가 수집한 transcript/bridge 본문 메시지 저장."""
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()
    if body.role_id != room.role_id:
        raise errors.room_role_mismatch()
    if body.source not in _MESSAGE_SOURCES:
        raise errors.WebguiError("invalid_source", 422, "Unsupported message source.")

    # agent_session 은 선택(hook 보강 전 transcript 는 미상일 수 있음). 있으면 소유 검증.
    session = None
    if body.agent_session_id:
        session = await repo.get_session(db, body.agent_session_id)
        if session is None:
            raise errors.WebguiError("agent_session_not_found", 404, "Agent session not found.")
        if str(session.room_id) != str(room.room_id):
            raise errors.WebguiError("session_room_mismatch", 409, "Session does not belong to room.")

    provider = getattr(body, "provider", None)
    record_id = getattr(body, "transcript_record_id", None)
    raw_hash = body.raw_hash or (
        "sha256:" + hashlib.sha256(
            f"{provider or ''}|{record_id or ''}|{room.role_id}|{body.normalized_text}".encode()
        ).hexdigest()
    )

    # 중복 방지 (DS-30 §5): transcript record_id 우선, 없으면 room+source+raw_hash
    if provider and record_id:
        existing = await repo.find_message_by_record(db, provider, record_id)
        if existing is not None:
            return {"message": message_to_dict(existing, project_id=room.project_id), "deduplicated": True}
    existing = await repo.find_message_by_hash(db, room.room_id, body.source, raw_hash)
    if existing is not None:
        return {"message": message_to_dict(existing, project_id=room.project_id), "deduplicated": True}

    is_inbound = body.message_type in _INBOUND_TYPES or body.message_type == "unmatched"
    direction = "inbound" if is_inbound else "outbound"

    # correlation 매칭
    correlation_id = body.correlation_id
    status = "received" if is_inbound else "sent"
    message_type = body.message_type
    if is_inbound and correlation_id is None:
        open_ob = await repo.find_open_outbound(db, room.room_id)
        if open_ob is not None:
            correlation_id = open_ob.correlation_id
        else:
            status = "unmatched"
            message_type = "unmatched"

    msg = await repo.create_message(
        db,
        room_id=room.room_id,
        agent_session_id=session.agent_session_id if session else None,
        correlation_id=correlation_id,
        role_id=room.role_id,
        surface_id=body.surface_id,
        team_session_id=room.team_session_id,   # provenance: FE 세션 구분선 (DV-41)
        direction=direction,
        source=body.source,
        message_type=message_type,
        provider=provider,
        transcript_path=mask_text(getattr(body, "transcript_path", None)),
        transcript_offset=getattr(body, "transcript_offset", None),
        transcript_record_id=record_id,
        raw_text=mask_text(body.raw_text),
        # tool-call 누출 차단(2026-06-10): 표시·저장되는 normalized 만 sanitize.
        # raw_text/raw_hash 는 원본 유지 → dedup 거동 불변.
        normalized_text=sanitize_tool_leak(body.normalized_text),
        raw_hash=raw_hash,
        status=status,
        occurred_at=body.occurred_at,
    )
    await repo.touch_room_last_message(db, room, msg, inbound=is_inbound)
    await db.commit()

    payload = message_to_dict(msg, transport="websocket", project_id=room.project_id)
    await hub.publish(
        str(room.room_id),
        {
            "type": "message_update",
            "cursor": f"{msg.recorded_at.isoformat()}|message:{msg.message_id}",
            "data": {
                "update_id": f"message:{msg.message_id}",
                "room_id": str(room.room_id),
                "correlation_id": str(correlation_id) if correlation_id else None,
                "update_type": "message_received" if is_inbound else "message_sent",
                "message": payload,
                "event": None,
                "occurred_at": msg.occurred_at.isoformat(),
            },
        },
        project_id=room.project_id,
    )
    return {"message": payload, "deduplicated": False}


async def collect_event(db: AsyncSession, room_id: str, body, collector: TranscriptCollector | None = None) -> dict:
    """WG-CHAT-06: hook/collector/cmux/read-screen runtime_event 저장 + transcript session 등록.

    hook_stop 수신 시 해당 에이전트 세션의 transcript 를 **즉시 1회 수집**한다(주 경로).
    background transcript_loop 폴링은 안전망 fallback 이다.
    방 라우팅(유저 확정 2026-06-08): AGENT_ID 1차 키, hook 이 POST 한 room_id 에만 저장.
    """
    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()

    event_type = body.event_type
    severity = body.severity
    norm = None
    # hook 이벤트는 normalizer 로 event_type 정규화 + transcript correlation hint 추출
    if body.source == "hook" and body.hook_provider:
        norm = hook_normalizer.normalize(
            body.hook_provider, body.hook_event_name or "", body.payload, body.severity
        )
        if not event_type:
            event_type = norm.event_type
        severity = norm.severity
        # transcript session 등록 (canonical 경로 — DS-60 §6.3/§7).
        # AGENT_ID 를 1차 키로, hook 이 특정한 room_id 를 세션에 묶어 그 방에만 저장되게 한다.
        if norm.session_id:
            if collector is None:
                collector = _build_default_collector()
            collector.sessions.register(
                provider=body.hook_provider,
                session_id=norm.session_id,
                project_id=room.project_id,
                role=room.role_id,
                transcript_path=norm.transcript_path,
                agent_id=norm.agent_id,
                room_id=str(room.room_id),
            )
    if not event_type:
        raise errors.WebguiError("invalid_event_type", 422, "event_type or hook_provider required.")

    masked = mask_payload(body.payload) if body.payload is not None else None
    ev = await repo.create_event(
        db,
        room_id=room.room_id,
        agent_session_id=body.agent_session_id,
        message_id=body.message_id,
        correlation_id=body.correlation_id,
        event_type=event_type,
        source=body.source,
        hook_provider=body.hook_provider,
        hook_event_name=body.hook_event_name,
        severity=severity,
        payload_json=masked,
        masked_payload_json=masked,
        occurred_at=body.occurred_at,
    )
    await db.commit()

    # hook_stop -> 해당 에이전트 세션 transcript 즉시 수집 (주 경로, DV-25 정정)
    # 신규 턴의 user/assistant 말풍선을 그 방에 저장 + WS push. 실패해도 폴링 fallback 이 재시도.
    if event_type == "hook_stop" and norm is not None and norm.session_id and collector is not None:
        try:
            await collector.collect_session(
                provider=body.hook_provider,
                session_id=norm.session_id,
                agent_id=norm.agent_id,
            )
        except Exception:  # noqa: BLE001  (수집 실패가 이벤트 저장/응답을 막지 않음)
            pass

    # hook_stop -> correlation_closed update (DS-40 §15.4)
    update_type = (
        "correlation_closed" if event_type == "hook_stop"
        else "runtime_error" if severity == "error"
        else None
    )
    if update_type:
        await hub.publish(
            str(room.room_id),
            {
                "type": "message_update",
                "cursor": f"{ev.recorded_at.isoformat()}|event:{ev.event_id}",
                "data": {
                    "update_id": f"event:{ev.event_id}",
                    "room_id": str(room.room_id),
                    "correlation_id": str(body.correlation_id) if body.correlation_id else None,
                    "update_type": update_type,
                    "message": None,
                    "event": {"event_type": ev.event_type, "source": ev.source, "severity": ev.severity},
                    "occurred_at": ev.occurred_at.isoformat(),
                },
            },
            project_id=room.project_id,
        )
    return {"event": event_to_dict(ev)}


async def collect_hook(db: AsyncSession, body, collector: TranscriptCollector | None = None) -> dict:
    """roomless hook 수집 (닭달걀 해소): room_id 없이 hook 을 처리한다 (QI-WG-022 정합).

    1) canonical 안정키 (project_id, role) 로 room upsert → commit. 1 역할 1 방.
       team_session_id / agent_id 는 방 식별키가 아니라 현재 세션·provenance 로 갱신한다.
       재부팅·agent 변화로 방을 새로 만들지 않고 이력을 유지한다.
    2) hook payload(session_id/transcript_path/agent_id/cwd)를 합쳐 CollectEventRequest 로 변환.
    3) 기존 collect_event 로직 재사용 → hook_stop 이면 그 방에 transcript 즉시수집.
    """
    room = await repo.upsert_room(
        db,
        project_id=body.project_id,
        role_id=body.role,
        display_name=body.display_name or body.role,
        room_type="pm" if body.role == "PM" else "role",
        team_session_id=body.team_session_id,
        agent_id=body.agent_id,
    )
    await db.commit()

    # provider: 계약 §2 hook_provider 1차, round-1 cli 별칭 fallback (둘 다 정규화)
    provider = _normalize_provider(body.hook_provider or body.cli)
    # hook hint payload: 계약 §2 hook_stdin 1차, round-1 payload 별칭 fallback. 명시 필드 병합 보존
    payload: dict = dict(body.hook_stdin or body.payload or {})
    if body.session_id:
        payload.setdefault("session_id", body.session_id)
    if body.transcript_path:
        payload.setdefault("transcript_path", body.transcript_path)
    if body.cwd:
        payload.setdefault("cwd", body.cwd)
    payload.setdefault("agent_id", body.agent_id)

    from ..schemas.collector import CollectEventRequest

    event_body = CollectEventRequest(
        source="hook",
        hook_provider=provider,
        hook_event_name=body.hook_event_name,
        severity=body.severity,
        payload=payload,
        occurred_at=body.occurred_at or _now(),
    )
    result = await collect_event(db, str(room.room_id), event_body, collector=collector)
    result["room_id"] = str(room.room_id)
    return result
