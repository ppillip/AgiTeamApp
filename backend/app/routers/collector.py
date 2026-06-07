"""Collector 내부 수집 API (DV-20.1): WG-CHAT-05/06.

브라우저 FE 는 호출하지 않는다. collector 전용 토큰으로만 접근한다 (DS-40 §21).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, require_collector_auth
from ..schemas.collector import CollectEventRequest, CollectMessageRequest
from ..schemas.common import ok
from ..services import collector_service

router = APIRouter(prefix="/api/webgui/internal/rooms", tags=["collector"])


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
