"""Collector 내부 수집 API (DV-20.1): WG-CHAT-05/06.

브라우저 FE 는 호출하지 않는다. collector 전용 토큰으로만 접근한다 (DS-40 §21).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, require_collector_auth
from ..schemas.collector import (
    CollectEventRequest,
    CollectMessageRequest,
    HookCollectRequest,
    RuntimeActivityCollectRequest,
)
from ..schemas.common import ok
from ..services import collector_service, runtime_activity_service

router = APIRouter(prefix="/api/webgui/internal/rooms", tags=["collector"])

# AGENT_ID 라우팅 hook 수집 (room_id 미지정 — 방은 백엔드가 upsert). 닭달걀 해소.
hook_router = APIRouter(prefix="/api/webgui/internal/hook", tags=["collector"])

# 요구사항 15-1 read-screen poller active pulse 수신 (DS-110 §6). FE 비호출, collector 전용 인증.
activity_router = APIRouter(prefix="/api/webgui/internal/runtime-activity", tags=["collector"])


@activity_router.post("/collect", dependencies=[Depends(require_collector_auth)])
async def collect_runtime_activity(body: RuntimeActivityCollectRequest, response: Response):
    # liveness 는 휘발성 — DB 미접근(room/event 저장 없음). get_db 의존성도 걸지 않는다.
    result = await runtime_activity_service.collect_runtime_activity(body)
    response.status_code = 201
    return ok(result)


@hook_router.post("/collect", dependencies=[Depends(require_collector_auth)])
async def collect_hook(body: HookCollectRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await collector_service.collect_hook(db, body)
    response.status_code = 201
    return ok(result)


@router.post("/{room_id}/messages/collect", dependencies=[Depends(require_collector_auth)])
async def collect_message(room_id: str, body: CollectMessageRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await collector_service.collect_message(db, room_id, body)
    response.status_code = 201
    return ok(result)


@router.post("/{room_id}/events/collect", dependencies=[Depends(require_collector_auth)])
async def collect_event(room_id: str, body: CollectEventRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await collector_service.collect_event(db, room_id, body)
    response.status_code = 201
    return ok(result)
