"""cmux 디스커버리 파서/레지스트리 테스트 (제우스 2026-06-07).

실측 `cmux tree --all` 출력 형식을 fixture 로 사용한다.
"""
from __future__ import annotations

from app.services.cmux_discovery import (
    DiscoveryRegistry,
    normalize_role,
    parse_title,
    parse_tree,
)

SAMPLE_TREE = '''window window:1 [current] ◀ active
├── workspace workspace:6 "Panthea" [selected] ◀ active
│   ├── pane pane:29 [focused] ◀ active
│   │   └── surface surface:29 [terminal] "제우스(PM)" [selected] ◀ active tty=ttys000
│   ├── pane pane:30
│   │   └── surface surface:30 [terminal] "불칸(BE)" [selected] ◀ here tty=ttys001
│   ├── pane pane:32
│   │   └── surface surface:32 [terminal] "이리스(FE)" [selected] tty=ttys003
│   ├── pane pane:33
│   │   └── surface surface:33 [terminal] "아르고스(QA)" [selected] tty=ttys004
│   ├── pane pane:31
│   │   └── surface surface:31 [terminal] "아테나(Architect)" [selected] tty=ttys002
│   ├── pane pane:34
│   │   └── surface surface:34 [terminal] "아틀라스(DevOps)" [selected] tty=ttys005
│   └── pane pane:35
│       └── surface surface:35 [terminal] "뮤즈(Designer)" [selected] tty=ttys006
├── workspace workspace:7 "~/Projects/Panthea"
│   └── pane pane:36 [focused]
│       └── surface surface:36 [terminal] "~/Projects/Panthea" [selected] tty=ttys015
└── workspace workspace:2 "AGI개발팀"
    ├── pane pane:8 [focused]
    │   └── surface surface:8 [terminal] "박피엠(PM)" [selected] tty=ttys007
    └── pane pane:9
        └── surface surface:9 [terminal] "박개발(BE)" [selected] tty=ttys009
'''


def test_parse_title():
    assert parse_title("제우스(PM)") == ("제우스", "PM")
    assert parse_title("아테나(Architect)") == ("아테나", "Architect")
    assert parse_title("불칸(BE)") == ("불칸", "DeveloperBE")
    assert parse_title("~/Projects/Panthea") is None  # 역할 괄호 없음
    assert parse_title("이서연") is None


def test_normalize_role():
    assert normalize_role("BE") == "DeveloperBE"
    assert normalize_role("fe") == "DeveloperFE"
    assert normalize_role("Architect") == "Architect"
    assert normalize_role("랜덤") is None


def test_parse_tree_projects_and_roles():
    projects = parse_tree(SAMPLE_TREE)
    by_name = {p.project_id: p for p in projects}
    # 역할 괄호가 있는 workspace 만 프로젝트로 인식 ("~/Projects/Panthea" 제외)
    assert set(by_name) == {"Panthea", "AGI개발팀"}
    roles = {s.role_id for s in by_name["Panthea"].surfaces}
    assert roles == {"PM", "DeveloperBE", "DeveloperFE", "QA", "Architect", "DevOps", "Designer"}
    pm = [s for s in by_name["Panthea"].surfaces if s.role_id == "PM"][0]
    assert pm.surface_id == "surface:29"
    assert pm.display_name == "제우스"
    assert pm.tty == "ttys000"


def test_parse_tree_prefers_runtime_metadata_project_and_agent():
    projects = parse_tree(
        SAMPLE_TREE,
        {
            "surface:8": {
                "project_id": "HookTest",
                "team_session_id": "20260608_121158",
                "agent_id": "PM",
                "agent_type": "claude",
            },
            "surface:9": {
                "project_id": "HookTest",
                "team_session_id": "20260608_121158",
                "agent_id": "DeveloperBE",
                "agent_type": "claude",
            },
        },
    )
    by_name = {p.project_id: p for p in projects}
    assert "HookTest" in by_name
    assert "AGI개발팀" not in by_name
    roles = {s.role_id: s for s in by_name["HookTest"].surfaces}
    assert roles["DeveloperBE"].surface_id == "surface:9"
    assert roles["DeveloperBE"].team_session_id == "20260608_121158"
    assert roles["DeveloperBE"].agent_id == "DeveloperBE"
    assert roles["DeveloperBE"].agent_type == "claude"


def test_registry_resolve_and_projects():
    reg = DiscoveryRegistry()
    reg.refresh_from_tree(SAMPLE_TREE)
    info = reg.resolve("Panthea", "DeveloperBE")
    assert info is not None
    assert info.surface_id == "surface:30"
    assert info.connection_state == "connected"
    projs = {p["project_id"]: p for p in reg.projects()}
    assert "Panthea" in projs and projs["Panthea"]["connected"] is True
    # PM 이 정렬상 맨 앞. 공개 키는 role (QI-WG-009)
    assert projs["Panthea"]["roles"][0]["role"] == "PM"
    # DS-40 ProjectSummary 원천 필드 (QI-WG-010)
    p = projs["Panthea"]
    assert p["connection_state"] == "connected"
    assert p["pm_connection_state"] == "connected"
    assert p["room_count"] == 7
    assert p["selected"] is True            # "Panthea" 워크스페이스가 ◀ active
    assert p["last_discovered_at"] is not None
    # 선택 프로젝트 id
    assert reg.selected_project_id() == "Panthea"


def test_registry_liveness_disconnect():
    reg = DiscoveryRegistry()
    reg.refresh_from_tree(SAMPLE_TREE)
    # 불칸(BE) 가 사라진 트리로 재갱신 → disconnected
    shrunk = SAMPLE_TREE.replace(
        '│   │   └── surface surface:30 [terminal] "불칸(BE)" [selected] ◀ here tty=ttys001\n', ""
    )
    reg.refresh_from_tree(shrunk)
    info = reg.resolve("Panthea", "DeveloperBE")
    assert info is not None
    assert info.connection_state == "disconnected"
    assert info.surface_id == "surface:30"  # 식별 정보는 보존
    # PM 은 여전히 connected
    assert reg.resolve("Panthea", "PM").connection_state == "connected"


def test_registry_missed_threshold_delays_disconnect():
    reg = DiscoveryRegistry()
    reg.refresh_from_tree(SAMPLE_TREE, missed_threshold=2)
    shrunk = SAMPLE_TREE.replace(
        '│   │   └── surface surface:30 [terminal] "불칸(BE)" [selected] ◀ here tty=ttys001\n', ""
    )
    first_changes = reg.refresh_from_tree(shrunk, missed_threshold=2)
    info = reg.resolve("Panthea", "DeveloperBE")
    assert info is not None
    assert info.connection_state == "connected"
    assert first_changes == []
    second_changes = reg.refresh_from_tree(shrunk, missed_threshold=2)
    assert reg.resolve("Panthea", "DeveloperBE").connection_state == "disconnected"
    assert second_changes[0]["to_state"] == "disconnected"
    assert second_changes[0]["reason"] == "cmux_tree_missed"
