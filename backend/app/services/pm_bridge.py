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
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from .. import errors
from ..config import Settings
from ..db import repositories as repo
from ..db.serializers import message_to_dict, provenance_dict
from .mux_port import MuxPort, get_mux_adapter
from .cmux_discovery import DiscoveryRegistry, registry as default_registry
from .events import hub
from .masking import mask_payload

PM_ROLE_ID = "PM"
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PMBridge:
    def __init__(
        self,
        settings: Settings,
        adapter: MuxPort | None = None,
        registry: DiscoveryRegistry | None = None,
    ) -> None:
        self.settings = settings
        # MuxPort 팩토리 경유(기본 cmux). 동작은 기존 CmuxAdapter 와 동일.
        self.adapter = adapter or get_mux_adapter(settings)
        self.registry = registry or default_registry

    async def _refresh_discovery(self) -> None:
        """on-demand cmux 디스커버리 갱신 (refresh-before-fail 용, DV-42).

        background discovery_loop 와 동일하게 cmux tree → registry 갱신. 실패해도
        조용히 넘긴다(호출자가 재해소 실패 시 surface_not_found 로 처리)."""
        try:
            tree = await self.adapter.tree()
            if not tree:
                return
            metadata = await self.adapter.runtime_metadata(tree)
            self.registry.refresh_from_tree(
                tree, metadata, missed_threshold=self.settings.discovery_missed_threshold
            )
        except Exception:  # noqa: BLE001
            return

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

        # cmux short refs are workspace-scoped. Refresh immediately before
        # resolving so the bridge uses a matching workspace_id + surface_id pair
        # even when duplicate project workspaces or stale registry entries exist.
        await self._refresh_discovery()

        # 1) PM surface 를 디스커버리 레지스트리에서 동적 해소 (surface 비의존).
        #    miss/disconnected 면 즉시 실패하지 않고 디스커버리를 1회 갱신 후 재해소한다
        #    (refresh-before-fail, QI-WG-023 / DV-42). 부팅 직후 레지스트리 미갱신 케이스 구제.
        info = self.registry.resolve(project_id, PM_ROLE_ID)
        if info is None or info.connection_state != "connected":
            logger.warning(
                "pm_bridge resolve miss project_id=%s info=%s",
                project_id,
                info,
            )
            await self._refresh_discovery()
            info = self.registry.resolve(project_id, PM_ROLE_ID)
            if info is None or info.connection_state != "connected":
                logger.warning("pm_bridge resolve failed after refresh project_id=%s info=%s", project_id, info)
                raise errors.surface_not_found()
        surface_id = info.surface_id
        workspace_id = info.workspace_id
        tty = info.tty
        display_name = info.display_name or "PM"

        # 2) 송신 직전 read-screen 핑으로 liveness 확정.
        #    실패 시에도 refresh-before-fail: surface 가 바뀌었을 수 있으니 재디스커버리 후 1회 재시도.
        ping_result = await self.adapter.read_screen(surface_id, lines=1, workspace_id=workspace_id, tty=tty)
        alive = ping_result["exit_code"] == 0
        if not alive:
            logger.warning(
                "pm_bridge ping failed project_id=%s surface_id=%s workspace_id=%s tty=%s exit_code=%s stdout=%r stderr=%r",
                project_id,
                surface_id,
                workspace_id,
                tty,
                ping_result.get("exit_code"),
                (ping_result.get("stdout") or "")[:200],
                (ping_result.get("stderr") or "")[:500],
            )
            self.registry.mark_disconnected(project_id, PM_ROLE_ID)
            await self._refresh_discovery()
            info = self.registry.resolve(project_id, PM_ROLE_ID)
            if info is None or info.connection_state != "connected":
                logger.warning("pm_bridge re-resolve failed project_id=%s info=%s", project_id, info)
                raise errors.surface_not_found()
            surface_id = info.surface_id
            workspace_id = info.workspace_id
            tty = info.tty
            display_name = info.display_name or "PM"
            ping_result = await self.adapter.read_screen(surface_id, lines=1, workspace_id=workspace_id, tty=tty)
            if ping_result["exit_code"] != 0:
                logger.warning(
                    "pm_bridge ping retry failed project_id=%s surface_id=%s workspace_id=%s tty=%s exit_code=%s stdout=%r stderr=%r",
                    project_id,
                    surface_id,
                    workspace_id,
                    tty,
                    ping_result.get("exit_code"),
                    (ping_result.get("stdout") or "")[:200],
                    (ping_result.get("stderr") or "")[:500],
                )
                # QI-WG-021/023: non-terminal surface 로 인한 구조적 실패면 진단 detail 첨부.
                # discovery terminal 필터로 재해소 시 보통 걸러지지만, 경합으로 남았을 때 방어.
                stderr = (ping_result.get("stderr") or "").lower()
                details = {"reason": "not_terminal"} if "not a terminal" in stderr else None
                raise errors.surface_not_found(details)

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
            team_session_id=room.team_session_id,   # provenance (DV-41)
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
        result = await self.adapter.submit(surface_id, clean, workspace_id, tty)
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
            masked_payload_json=mask_payload(
                {"submitted": submitted, "surface_id": surface_id, "workspace_id": workspace_id}
            ),
            occurred_at=_now(),
        )
        await db.commit()

        # DS-40 §7.2 공개계약(QI-WG-029): message 에 project_id + provenance(transport=rest).
        message = message_to_dict(msg, transport="rest", project_id=project_id)
        # 프론트 dedup: WS broadcast message 에도 client_message_id 를 포함시켜
        # 낙관적(optimistic) 말풍선과 서버 말풍선을 상관시켜 중복 표시를 방지한다.
        message["client_message_id"] = client_message_id
        ack = {
            "accepted": True,
            "send_submitted": submitted,
            "message_id": str(msg.message_id),
            "correlation_id": str(correlation_id),
            "project_id": project_id,                # DS-40 §7.2 공개계약 (QI-WG-029)
            "room_id": str(room.room_id),
            "role": PM_ROLE_ID,
            "surface_id": surface_id,
            "workspace_id": workspace_id,            # 내부 디버그값(호환 유지)
            "agent_session_id": None,
            "status": msg.status,
            # ack provenance: PM bridge 가 REST 로 제출한 실데이터 (DS-40 §7.2)
            "provenance": provenance_dict("pm_bridge", runtime_state="live", transport="rest"),
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
            project_id=room.project_id,
        )

        if not submitted:
            raise errors.send_failed({"message_id": str(msg.message_id)})

        return {"ack": ack, "message": message}
