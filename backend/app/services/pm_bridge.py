"""PM 브릿지 (DV-20.1 + 2026-06-07 보강).

설계: DS-20 §10.1/§11.3, DS-40 §7, DS-60 §5, 모니터 아키텍처 쉬운그림 v0.1.

[라우팅 확정 — 제우스 2026-06-07]
- 오케스트레이터 = 웹사용자(휴먼). 모든 웹 발신은 PM surface 로만 전달된다.
- 팀원 surface 로 직접 send 하는 경로는 제공하지 않는다.
- cmux 송신 대상은 항상 PM surface 고정.

[surface 비의존 — 제우스 2026-06-07]
- 식별/저장 키는 (project_id, role). surface_id 는 송신 직전 디스커버리 레지스트리에서
  동적 해소하는 일시값이다. surface 가 바뀌어도 식별/저장은 깨지지 않는다.
- 송신 직전 read-screen 핑으로 liveness 를 확정한다.

저장 순서 (DS-60 §5.2): outbound pending insert -> commit -> cmux submit ->
status update + cmux_send_result event -> commit -> WebSocket publish.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from .. import errors
from ..config import Settings
from ..db import repositories as repo
from ..db.serializers import message_to_dict
from .cmux_adapter import CmuxAdapter
from .cmux_discovery import DiscoveryRegistry, registry as default_registry
from .events import hub
from .masking import mask_payload

PM_ROLE_ID = "PM"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PMBridge:
    def __init__(
        self,
        settings: Settings,
        adapter: CmuxAdapter | None = None,
        registry: DiscoveryRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter or CmuxAdapter(settings.cmux_bin, settings.cmux_timeout_seconds)
        self.registry = registry or default_registry

    async def send(
        self,
        db: AsyncSession,
        *,
        project_id: str,
        text: str,
        client_message_id: str | None = None,
    ) -> dict:
        clean = (text or "").strip()
        if not clean:
            raise errors.empty_message()

        # 1) PM surface 를 디스커버리 레지스트리에서 동적 해소 (surface 비의존)
        info = self.registry.resolve(project_id, PM_ROLE_ID)
        if info is None or info.connection_state != "connected":
            raise errors.surface_not_found()
        surface_id = info.surface_id
        display_name = info.display_name or "PM"

        # 2) 송신 직전 read-screen 핑으로 liveness 확정
        alive = await self.adapter.ping(surface_id)
        if not alive:
            self.registry.mark_disconnected(project_id, PM_ROLE_ID)
            raise errors.surface_not_found()

        # 3) PM 방 upsert (식별 키 = project_id, role). surface 는 일시값으로만 기록.
        room = await repo.upsert_room(
            db,
            project_id=project_id,
            role_id=PM_ROLE_ID,
            display_name=display_name,
            room_type="pm",
        )
        room.current_surface_id = surface_id
        room.ready_state = "ready"

        # 4) outbound pending 선저장
        correlation_id = uuid.uuid4()
        msg = await repo.create_message(
            db,
            room_id=room.room_id,
            correlation_id=correlation_id,
            role_id=PM_ROLE_ID,
            surface_id=surface_id,
            direction="outbound",
            source="webgui",
            message_type="user_message",
            raw_text=clean,
            normalized_text=clean,
            status="pending",
            occurred_at=_now(),
        )
        await db.commit()

        # 5) cmux submit (DB transaction 밖)
        result = await self.adapter.submit(surface_id, clean)
        submitted = bool(result.get("submitted"))

        msg.status = "sent" if submitted else "failed"
        msg.updated_at = _now()
        await repo.touch_room_last_message(db, room, msg, inbound=False)
        await repo.create_event(
            db,
            room_id=room.room_id,
            message_id=msg.message_id,
            correlation_id=correlation_id,
            event_type="cmux_send_result",
            source="cmux_adapter",
            severity="info" if submitted else "error",
            payload_json=mask_payload(result),
            masked_payload_json=mask_payload({"submitted": submitted, "surface_id": surface_id}),
            occurred_at=_now(),
        )
        await db.commit()

        message = message_to_dict(msg)
        ack = {
            "accepted": True,
            "send_submitted": submitted,
            "message_id": str(msg.message_id),
            "correlation_id": str(correlation_id),
            "room_id": str(room.room_id),
            "role": PM_ROLE_ID,
            "surface_id": surface_id,
            "agent_session_id": None,
            "status": msg.status,
            "client_message_id": client_message_id,
            "submitted_at": result.get("ended_at"),
        }

        await hub.publish(
            str(room.room_id),
            {
                "type": "message_update",
                "cursor": f"{msg.recorded_at.isoformat()}|message:{msg.message_id}",
                "data": {
                    "update_id": f"message:{msg.message_id}",
                    "room_id": str(room.room_id),
                    "correlation_id": str(correlation_id),
                    "update_type": "message_sent" if submitted else "message_failed",
                    "message": message,
                    "event": None,
                    "occurred_at": msg.occurred_at.isoformat(),
                },
            },
        )

        if not submitted:
            raise errors.send_failed({"message_id": str(msg.message_id)})

        return {"ack": ack, "message": message}
