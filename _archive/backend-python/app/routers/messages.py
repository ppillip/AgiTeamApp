"""메시지 채널 API (DV-20.1): WG-MSG-02/03/04 + WG-MSG-05 WebSocket.

[라우팅 확정] WG-MSG-02 송신은 항상 PM surface 로 전달된다 (제우스 2026-06-07).
요청 body 의 room_id/role_id 는 호환을 위해 수용하되, cmux 송신 대상은 PM 고정이다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
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
    attachments = (
        [{"attachment_id": a.attachment_id} for a in body.attachments] if body.attachments else None
    )
    result = await bridge.send(
        db,
        project_id=project_id,
        text=body.text,
        client_message_id=body.client_message_id,
        attachments=attachments,
    )
    response.status_code = 201
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
    # DS-40 §8 공개계약(QI-WG-029): message.project_id 채움 — 소속 room 에서 해소.
    room = await repo.get_room(db, msg.room_id)
    pid = room.project_id if room else None
    return ok({"message": message_to_dict(msg, project_id=pid), "related_updates": related})


def _parse_after_cursor(after: str | None) -> datetime | None:
    """polling `after` 파라미터를 datetime 으로 해소.

    이 엔드포인트가 내보내는 next_cursor 는 복합 포맷
    ``"{recorded_at_iso}|message:{message_id}"`` 이다. FE 가 그 커서를 그대로 되돌려
    보내므로(폴링 폴백), 복합 커서를 받아 시각부분만 파싱한다. message_updates 는
    datetime 만 필요하므로 id 부분은 버린다. 순수 ISO 시각도 그대로 허용한다.
    파싱 실패 시 422(invalid_pagination)로 명확히 거절한다(무한 400 루프 방지, QI-WG).
    """
    from .. import errors

    if after is None or after == "":
        return None
    ts_part = after.partition("|message:")[0] if "|message:" in after else after
    try:
        return datetime.fromisoformat(ts_part)
    except (ValueError, TypeError):
        raise errors.WebguiError("invalid_pagination", 422, "invalid after cursor format")


@router.get("/message-updates", dependencies=[Depends(require_auth)])
async def message_updates(
    room_id: str = Query(...),
    project_id: str = Query(...),
    after: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """WG-MSG-04 polling fallback. message + runtime_event 를 MessageUpdate 로 합성.

    project_id 는 프로젝트 격리 방어키다(DS-40 §9.1 필수). 조회한 room 이 해당
    project 에 속하지 않으면 cross-project 누수를 막기 위해 room_not_found(404)로
    은닉 거절한다(존재 여부 비노출, DS-40 §21). A-F1 결함수정 2026-06-10:
    아테나 원설계 판정 = API 경계에서 project_id 방어검증 강제(UUID 신뢰 완화 아님).
    """
    from .. import errors

    after_dt = _parse_after_cursor(after)
    room = await repo.get_room(db, room_id)
    if room is None or room.project_id != project_id:
        raise errors.room_not_found()
    msgs = await repo.updates_since(db, room.room_id, after_dt, limit)
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
                "message": message_to_dict(m, transport="polling", project_id=room.project_id),
                "event": None,
                "occurred_at": m.occurred_at,
            }
        )
    next_cursor = (
        f"{msgs[-1].recorded_at.isoformat()}|message:{msgs[-1].message_id}" if msgs else None
    )
    return ok({"updates": updates, "next_cursor": next_cursor})


def _message_update_payload(m, project_id: str | None = None) -> dict:
    """WS push/replay 공통 MessageUpdate envelope.

    collector/pm_bridge 의 hub.publish payload 와 동일 형태로 만들어, gap replay 와
    실시간 push 를 FE 가 구분 없이 처리하게 한다(QI-WG-030).
    """
    ut = (
        "message_received"
        if m.direction == "inbound"
        else "message_sent"
        if m.status == "sent"
        else "message_failed"
        if m.status == "failed"
        else "message_streaming"
    )
    return {
        "type": "message_update",
        "cursor": f"{m.recorded_at.isoformat()}|message:{m.message_id}",
        "data": {
            "update_id": f"message:{m.message_id}",
            "room_id": str(m.room_id),
            "correlation_id": str(m.correlation_id) if m.correlation_id else None,
            "update_type": ut,
            "message": message_to_dict(m, transport="websocket", project_id=project_id),
            "event": None,
            "occurred_at": m.occurred_at,
        },
    }


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

    # WG-MSG-05 프로젝트 격리(A-F1 후속, message-updates 와 동일 계약): project_id 필수.
    # project_id 없는 구독은 hub 전역 구독이 되어 타 프로젝트 push 까지 받으므로 거절한다.
    project_id = ws.query_params.get("project_id")
    if not project_id:
        await ws.close(code=4400)  # missing project_id
        return
    room_param = ws.query_params.get("room_id")
    # room_id 지정 시 그 방이 project 에 속하는지 방어검증(타 프로젝트 방 구독 차단).
    # 존재 여부 비노출을 위해 미존재·불일치 모두 동일하게 거절한다(DS-40 §21).
    if room_param:
        from ..db.base import get_sessionmaker

        async with get_sessionmaker()() as db:
            room = await repo.get_room(db, room_param)
        if room is None or room.project_id != project_id:
            await ws.close(code=4404)  # room not found / cross-project
            return
    await ws.accept()
    rooms: set[str] | None = {room_param} if room_param else None
    q = await hub.register(rooms, project_id)
    # QI-WG-030: 재연결 gap replay (DS-40 §10 / DS-60 §8.4). room 지정 + after cursor 면
    # 연결 직후 그 cursor 이후 메시지를 먼저 흘려보내 끊긴 구간을 복구한다. 전역 구독
    # (room 미지정)은 replay 대상이 모호하므로 생략하고 실시간 push 만 받는다.
    after = ws.query_params.get("after")
    if room_param and after:
        from .. import errors as _errs
        from ..db.base import get_sessionmaker

        try:
            after_dt = _parse_after_cursor(after)
        except _errs.WebguiError:
            after_dt = None  # 깨진 cursor 는 replay 생략(실시간만), WS 는 끊지 않는다
        if after_dt is not None:
            async with get_sessionmaker()() as db:
                missed = await repo.updates_since(db, room_param, after_dt, 200)
            for m in missed:
                try:
                    await ws.send_json(jsonable_encoder(_message_update_payload(m, project_id)))
                except Exception:
                    break
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
                    # payload 에 datetime 등 비-JSON 타입이 섞여 있어도 안전하게 직렬화
                    # (이전엔 ws.send_json 의 json.dumps 가 datetime 에서 실패→조용히 유실, QI-WG-026)
                    await ws.send_json(jsonable_encoder(payload))
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
