"""멀티프로젝트 디스커버리 API (제우스 2026-06-07, QI-WG-010 / QI-WG-021 정정).

GET /api/webgui/projects — 프로젝트(팀) 목록을 DS-40 ProjectSummary 로 반환.
원천 = cmux 디스커버리 ∪ DB(실제 방 보유 project). cmux 에 안 떠도 DB 에 방/메시지가
있는 프로젝트(예: hook E2E 로 생성된 HookTest)를 선택 가능하게 노출한다 (QI-WG-021).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import repositories as repo
from ..deps import get_db, require_auth
from ..schemas.common import ok
from ..services.cmux_discovery import registry

router = APIRouter(prefix="/api/webgui", tags=["projects"])


@router.get("/projects", dependencies=[Depends(require_auth)])
async def list_projects(request: Request, db: AsyncSession = Depends(get_db)):
    reg = getattr(request.app.state, "registry", registry)
    settings = get_settings()
    raw = reg.projects()
    projects = []
    seen: set[str] = set()
    for p in raw:
        seen.add(p["project_id"])
        projects.append(
            {
                "project_id": p["project_id"],
                "workspace_id": p.get("workspace_id"),
                "workspace_title": p.get("workspace_title", p["project_id"]),
                "root_path": str(settings.project_root(p["project_id"])),
                "connection_state": p.get("connection_state", "disconnected"),
                "pm_connection_state": p.get("pm_connection_state", "absent"),
                "room_count": p.get("room_count", len(p.get("roles", []))),
                "selected": p.get("selected", False),
                "last_discovered_at": p.get("last_discovered_at"),
                "roles": p.get("roles", []),
            }
        )

    # DB 에 방을 보유했으나 디스커버리에 안 뜬 프로젝트를 보강 (QI-WG-021)
    try:
        db_projects = await repo.distinct_projects_with_rooms(db)
    except Exception:  # noqa: BLE001  (DB 미가동 시에도 디스커버리 결과는 반환)
        db_projects = []
    for dp in db_projects:
        if dp["project_id"] in seen:
            continue
        seen.add(dp["project_id"])
        projects.append(
            {
                "project_id": dp["project_id"],
                "workspace_id": None,
                "workspace_title": dp["project_id"],
                "root_path": str(settings.project_root(dp["project_id"])),
                "connection_state": "disconnected",
                "pm_connection_state": "absent",
                "room_count": dp["room_count"],
                "selected": False,
                "last_discovered_at": None,
                "roles": dp["roles"],
            }
        )

    return ok(
        {
            "selected_project_id": reg.selected_project_id(),
            "projects": projects,
        }
    )
