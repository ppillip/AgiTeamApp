"""WG-MSG-01 런타임 상태 조회 (DS-40 §6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import repositories as repo
from ..db.serializers import runtime_context_dict
from ..deps import get_db, require_auth
from ..schemas.common import ok
from ..services.cmux_discovery import registry
from datetime import datetime, timezone

router = APIRouter(prefix="/api/webgui", tags=["runtime"])


def _connection_state(project_id: str, role_id: str) -> str:
    info = registry.resolve(project_id, role_id)
    return info.connection_state if info else "disconnected"


@router.get("/runtime/status", dependencies=[Depends(require_auth)])
async def runtime_status(
    project_id: str | None = Query(default=None),
    room_id: str | None = Query(default=None),
    role_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    pid = project_id or settings.project_id
    rooms = await repo.list_rooms(db, pid)
    if room_id:
        rooms = [r for r in rooms if str(r.room_id) == room_id]
    if role_id:
        rooms = [r for r in rooms if r.role_id == role_id]
    out = []
    for r in rooms:
        session = await repo.active_session_for_room(db, r.room_id)
        collector_state = session.collector_state if session else "unknown"
        ctx = runtime_context_dict(r, collector_state)
        ctx["connection_state"] = _connection_state(pid, r.role_id)
        out.append(ctx)
    return ok(
        {
            "project_id": pid,
            "server_time": datetime.now(timezone.utc),
            "rooms": out,
        }
    )
