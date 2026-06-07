"""멀티프로젝트 디스커버리 API (제우스 2026-06-07, QI-WG-010 정정).

GET /api/webgui/projects — cmux 에 떠 있는 프로젝트(팀) 목록을 DS-40 ProjectSummary 로 반환.
DB 비의존: 디스커버리 레지스트리 + 설정(project_root)만으로 응답한다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..config import get_settings
from ..deps import require_auth
from ..schemas.common import ok
from ..services.cmux_discovery import registry

router = APIRouter(prefix="/api/webgui", tags=["projects"])


@router.get("/projects", dependencies=[Depends(require_auth)])
async def list_projects(request: Request):
    reg = getattr(request.app.state, "registry", registry)
    settings = get_settings()
    raw = reg.projects()
    projects = []
    for p in raw:
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
    return ok(
        {
            "selected_project_id": reg.selected_project_id(),
            "projects": projects,
        }
    )
