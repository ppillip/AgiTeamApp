"""프로젝트 디스커버리 스키마 (DS-40 ProjectSummary, QI-WG-010)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ProjectRoleSummary(BaseModel):
    role: str                 # 공개 응답 필드 (QI-WG-009)
    display_name: str
    surface_id: str | None = None
    connection_state: str     # connected | disconnected
    last_seen_at: datetime | None = None


class ProjectSummary(BaseModel):
    project_id: str
    workspace_id: str | None = None
    workspace_title: str
    root_path: str
    connection_state: str             # connected | disconnected (프로젝트 종합)
    pm_connection_state: str          # connected | disconnected | absent
    room_count: int
    selected: bool = False
    last_discovered_at: datetime | None = None
    roles: list[ProjectRoleSummary] = []


class ProjectsResponse(BaseModel):
    selected_project_id: str | None = None
    projects: list[ProjectSummary] = []
