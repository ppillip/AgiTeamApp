"""메시지 채널 API (DV-20.1): WG-MSG-02/03/04 + WG-MSG-05 WebSocket.

[라우팅 확정] WG-MSG-02 송신은 항상 PM surface 로 전달된다 (제우스 2026-06-07).
요청 body 의 room_id/role_id 는 호환을 위해 수용하되, cmux 송신 대상은 PM 고정이다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import repositories as repo
from ..db.serializers import message_to_dict
from ..deps import get_db, require_auth
from ..schemas.common import ok
from ..schemas.message import SendMessageRequest
from ..services.events import hub
from ..services.pm_bridge import PMBridge

router = APIRouter(prefix="/api/webgui", tags=["messages"])


@router.post("/messages", dependencies=[Depends(require_auth)])
async def send_message(body: SendMessageRequest, response: Response, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    project_id = body.project_id or settings.project_id
    bridge = PMBridge(settings)
    result = await bridge.send(
        db, project_id=project_id, text=body.text, client_message_id=body.client_message_id
    )
    response.status_code = 202
    return ok(result)


@router.get("/messages/{message_id}", dependencies=[Depends(require_auth)])
async def get_message(message_id: str, db: AsyncSession = Depends(get_db)):
    from .. import errors

    msg = await repo.get_message(db, message_id)
    if msg is None:
        raise errors.message_not_found()
    related = []
    if msg.correlation_id is not None:
        events = await repo.list_events(db, msg.room_id, correlation_id=msg.correlation_id)
        for e in events:
            related.append(
                {
                    "update_id": f"event:{e.event_id}",
                    "room_id": str(e.room_id),
                    "correlation_id": str(e.correlation_id) if e.correlation_id else None,
                    "update_type": "correlation_closed" if e.event_type == "hook_stop" else "runtime_error",
                    "message": None,
                    "event": {"event_type": e.event_type, "severity": e.severity},
                    "occurred_at": e.occurred_at,
                }
            )
    return ok({"message": message_to_dict(msg), "related_updates": related})


@router.get("/message-updates", dependencies=[Depends(require_auth)])
async def message_updates(
    room_id: str = Query(...),
    after: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """WG-MSG-04 polling fallback. message + runtime_event 를 MessageUpdate 로 합성."""
    from .. import errors

    room = await repo.get_room(db, room_id)
    if room is None:
        raise errors.room_not_found()
    msgs = await repo.updates_since(db, room.room_id, after, limit)
    updates = []
    for m in msgs:
        ut = "message_received" if m.direction == "inbound" else (
            "message_sent" if m.status == "sent" else "message_failed" if m.status == "failed" else "message_streaming"
        )
        updates.append(
            {
                "update_id": f"message:{m.message_id}",
                "room_id": str(m.room_id),
                "correlation_id": str(m.correlation_id) if m.correlation_id else None,
                "update_type": ut,
                "message": message_to_dict(m),
                "event": None,
                "occurred_at": m.occurred_at,
            }
        )
    next_cursor = (
        f"{msgs[-1].recorded_at.isoformat()}|message:{msgs[-1].message_id}" if msgs else None
    )
    return ok({"updates": updates, "next_cursor": next_cursor})


@router.websocket("/message-stream")
async def message_stream(ws: WebSocket):
    """WG-MSG-05 실시간 update channel (DS-40 §10).

    인증: api_token 설정 시 ?token= 또는 Authorization 헤더로 검증.
    """
    settings = get_settings()
    if settings.auth_required:
        token = ws.query_params.get("token")
        if not token:
            auth = ws.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if token != settings.api_token:
            await ws.close(code=4401)
            return

    await ws.accept()
    room_param = ws.query_params.get("room_id")
    rooms: set[str] | None = {room_param} if room_param else None
    q = await hub.register(rooms)
    try:
        while True:
            push_task = asyncio.create_task(q.get())
            recv_task = asyncio.create_task(ws.receive_json())
            done, pending = await asyncio.wait(
                {push_task, recv_task}, timeout=20, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if not done:
                await ws.send_json({"type": "heartbeat"})
                continue
            if push_task in done and not push_task.cancelled():
                try:
                    payload = push_task.result()
                    await ws.send_json(payload)
                except Exception:
                    pass
            if recv_task in done and not recv_task.cancelled():
                try:
                    msg = recv_task.result()
                except Exception:
                    break
                if isinstance(msg, dict) and msg.get("type") == "subscribe":
                    new_rooms = msg.get("rooms")
                    await hub.update_subscription(q, set(new_rooms) if new_rooms else None)
                elif isinstance(msg, dict) and msg.get("type") == "unsubscribe":
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(q)
