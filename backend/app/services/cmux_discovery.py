"""cmux 디스커버리 (제우스 2026-06-07 확정).

`cmux tree --all` 출력을 파싱해 (project_id, role) → surface 매핑을 백엔드가 자체 구성한다.
AgiTeam(agiteam.sh)은 건드리지 않는다 — 모니터는 떠 있는 팀을 '발견'만 한다.

tree 출력 예:
    ├── workspace workspace:6 "Panthea" [selected] ◀ active
    │   ├── pane pane:29 [focused] ◀ active
    │   │   └── surface surface:29 [terminal] "제우스(PM)" [selected] ◀ active tty=ttys000

규칙:
- workspace 이름 = project_id (예: "Panthea")
- surface title "이름(역할토큰)" 에서 역할 추출 → 정규 role_id 로 매핑
- 역할 토큰이 인식되지 않는 surface(경로 터미널 등)는 무시
- 인식된 역할이 1개 이상인 workspace 만 '프로젝트' 로 취급
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

# surface title 괄호 안 토큰 → 정규 role_id
ROLE_TOKEN_MAP: dict[str, str] = {
    "pm": "PM",
    "architect": "Architect",
    "arch": "Architect",
    "be": "DeveloperBE",
    "developerbe": "DeveloperBE",
    "devbe": "DeveloperBE",
    "backend": "DeveloperBE",
    "fe": "DeveloperFE",
    "developerfe": "DeveloperFE",
    "devfe": "DeveloperFE",
    "frontend": "DeveloperFE",
    "qa": "QA",
    "designer": "Designer",
    "design": "Designer",
    "devops": "DevOps",
    "ops": "DevOps",
}

_WS_RE = re.compile(r'workspace\s+(workspace:\S+)\s+"([^"]+)"')
_SURFACE_RE = re.compile(r'surface\s+(surface:\S+)\s+\[[^\]]*\]\s+"([^"]+)"')
_TITLE_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_role(token: str) -> str | None:
    return ROLE_TOKEN_MAP.get(token.strip().lower())


def parse_title(title: str) -> tuple[str, str] | None:
    """'제우스(PM)' → ('제우스', 'PM'). 인식 불가 시 None."""
    m = _TITLE_RE.match(title.strip())
    if not m:
        return None
    display_name = m.group(1).strip()
    role = normalize_role(m.group(2))
    if not role:
        return None
    return display_name or role, role


@dataclass
class DiscoveredSurface:
    project_id: str
    role_id: str
    surface_id: str
    display_name: str


@dataclass
class DiscoveredProject:
    project_id: str
    workspace_id: str
    workspace_title: str
    selected: bool = False
    surfaces: list[DiscoveredSurface] = field(default_factory=list)


def parse_tree(text: str) -> list[DiscoveredProject]:
    """cmux tree --all 출력 → 프로젝트 목록 (인식된 역할이 있는 workspace 만).

    workspace 라인에 '◀ active' 마커가 있으면 selected(=현재 활성 워크스페이스)로 본다.
    """
    projects: dict[str, DiscoveredProject] = {}
    current_ws_id: str | None = None
    current_proj: str | None = None
    current_selected = False
    for line in text.splitlines():
        ws = _WS_RE.search(line)
        if ws:
            current_ws_id = ws.group(1)
            current_proj = ws.group(2).strip()
            current_selected = "◀ active" in line
            continue
        sf = _SURFACE_RE.search(line)
        if sf and current_proj is not None:
            surface_id = sf.group(1)
            parsed = parse_title(sf.group(2))
            if parsed is None:
                continue
            display_name, role = parsed
            proj = projects.get(current_proj)
            if proj is None:
                proj = DiscoveredProject(
                    project_id=current_proj,
                    workspace_id=current_ws_id or "",
                    workspace_title=current_proj,
                    selected=current_selected,
                )
                projects[current_proj] = proj
            proj.surfaces.append(
                DiscoveredSurface(current_proj, role, surface_id, display_name)
            )
    return list(projects.values())


@dataclass
class SurfaceInfo:
    project_id: str
    role_id: str
    surface_id: str
    display_name: str
    connection_state: str  # connected | disconnected
    last_seen_at: datetime
    workspace_id: str = ""


class DiscoveryRegistry:
    """(project_id, role_id) → SurfaceInfo 인메모리 레지스트리.

    surface_id 는 송신 직전 동적 해소를 위한 일시값. 식별/저장 키는 (project_id, role_id).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[tuple[str, str], SurfaceInfo] = {}
        # project_id -> {workspace_id, workspace_title, selected}
        self._proj_meta: dict[str, dict] = {}
        self._selected_project: str | None = None
        self._last_refresh: datetime | None = None

    def refresh_from_tree(self, tree_text: str) -> None:
        projects = parse_tree(tree_text)
        present: set[tuple[str, str]] = set()
        now = _now()
        with self._lock:
            self._selected_project = None
            for proj in projects:
                self._proj_meta[proj.project_id] = {
                    "workspace_id": proj.workspace_id,
                    "workspace_title": proj.workspace_title,
                    "selected": proj.selected,
                }
                if proj.selected:
                    self._selected_project = proj.project_id
                for s in proj.surfaces:
                    key = (s.project_id, s.role_id)
                    present.add(key)
                    self._map[key] = SurfaceInfo(
                        project_id=s.project_id,
                        role_id=s.role_id,
                        surface_id=s.surface_id,
                        display_name=s.display_name,
                        connection_state="connected",
                        last_seen_at=now,
                        workspace_id=proj.workspace_id,
                    )
            # 직전에 있었으나 이번에 사라진 surface → disconnected (식별 정보는 보존)
            for key, info in self._map.items():
                if key not in present:
                    info.connection_state = "disconnected"
            self._last_refresh = now

    def resolve(self, project_id: str, role_id: str) -> SurfaceInfo | None:
        with self._lock:
            return self._map.get((project_id, role_id))

    def mark_disconnected(self, project_id: str, role_id: str) -> None:
        with self._lock:
            info = self._map.get((project_id, role_id))
            if info:
                info.connection_state = "disconnected"

    def selected_project_id(self) -> str | None:
        with self._lock:
            return self._selected_project

    def projects(self) -> list[dict]:
        """프로젝트별 원천 데이터(역할 목록 포함). DS-40 ProjectSummary 변환은 router 가 수행."""
        with self._lock:
            grouped: dict[str, dict] = {}
            for (proj, role), info in self._map.items():
                meta = self._proj_meta.get(proj, {})
                g = grouped.setdefault(
                    proj,
                    {
                        "project_id": proj,
                        "workspace_id": meta.get("workspace_id", info.workspace_id),
                        "workspace_title": meta.get("workspace_title", proj),
                        "selected": meta.get("selected", False),
                        "roles": [],
                    },
                )
                g["roles"].append(
                    {
                        "role": role,
                        "display_name": info.display_name,
                        "surface_id": info.surface_id,
                        "connection_state": info.connection_state,
                        "last_seen_at": info.last_seen_at,
                    }
                )
            for g in grouped.values():
                g["roles"].sort(key=lambda r: _role_order(r["role"]))
                roles = g["roles"]
                g["connected"] = any(r["connection_state"] == "connected" for r in roles)
                g["connection_state"] = "connected" if g["connected"] else "disconnected"
                pm = next((r for r in roles if r["role"] == "PM"), None)
                g["pm_connection_state"] = pm["connection_state"] if pm else "absent"
                g["room_count"] = len(roles)
                last_seen = [r["last_seen_at"] for r in roles if r["last_seen_at"]]
                g["last_discovered_at"] = max(last_seen) if last_seen else None
            return list(grouped.values())

    def roles_for(self, project_id: str) -> list[SurfaceInfo]:
        with self._lock:
            return [info for (p, _), info in self._map.items() if p == project_id]


_ROLE_ORDER = ["PM", "Architect", "DeveloperBE", "DeveloperFE", "Designer", "QA", "DevOps"]


def _role_order(role_id: str) -> int:
    return _ROLE_ORDER.index(role_id) if role_id in _ROLE_ORDER else 99


registry = DiscoveryRegistry()
