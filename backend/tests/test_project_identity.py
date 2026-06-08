"""DV-49/QI-WG-027: 프로젝트 식별·표시 = 실재 폴더명. 유령(폴더 부재) 제외, workspace_title 금지."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _mk_project(root):
    (root / ".agiteam").mkdir(parents=True, exist_ok=True)


def test_config_project_exists_and_display(tmp_path, monkeypatch):
    real = tmp_path / "HookTest"
    _mk_project(real)
    ghost = tmp_path / "AGI개발팀"  # 생성 안 함 → 유령
    roots = {"HookTest": str(real), "AGI개발팀": str(ghost)}
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps(roots))
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.project_exists("HookTest") is True
    assert s.project_exists("AGI개발팀") is False     # root 부재 → 유령
    assert s.project_display_name("HookTest") == "HookTest"   # 폴더 basename
    get_settings.cache_clear()


def test_projects_endpoint_excludes_ghosts_and_uses_folder_name(tmp_path, monkeypatch):
    # 실재 2개(폴더명 그대로 '2' 포함) + 유령 2개
    real_two = tmp_path / "2"          # 폴더명이 '2' 여도 실재면 정상 프로젝트 (판단 금지)
    _mk_project(real_two)
    real_panthea = tmp_path / "Panthea"
    _mk_project(real_panthea)
    roots = {
        "2": str(real_two),
        "Panthea": str(real_panthea),
        "AGI개발팀": str(tmp_path / "AGI개발팀"),     # 유령(미생성)
        "HookOptCollect": str(tmp_path / "HookOptCollect"),  # 유령(미생성)
    }
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps(roots))
    monkeypatch.setenv("WEBGUI_PROJECT_ID", "Panthea")
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    monkeypatch.setenv("WEBGUI_ENABLE_BACKGROUND", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    # 디스커버리 레지스트리에 실재 2개 + 유령 2개를 surface 로 주입
    from app.services.cmux_discovery import registry
    tree = (
        'workspace ws1 "팀A"\n'
        '  surface s1 [x] "제우스(PM)" tty=ttys001\n'
        'workspace ws2 "팀B"\n'
        '  surface s2 [x] "박개발(DeveloperBE)" tty=ttys002\n'
        'workspace ws3 "팀유령1"\n'
        '  surface s3 [x] "제우스(PM)" tty=ttys003\n'
        'workspace ws4 "팀유령2"\n'
        '  surface s4 [x] "제우스(PM)" tty=ttys004\n'
    )
    metadata = {
        "s1": {"project_id": "2", "role": "PM"},
        "s2": {"project_id": "Panthea", "role": "DeveloperBE"},
        "s3": {"project_id": "AGI개발팀", "role": "PM"},
        "s4": {"project_id": "HookOptCollect", "role": "PM"},
    }
    registry.refresh_from_tree(tree, metadata)

    from app.main import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/webgui/projects")
        assert r.status_code == 200
        data = r.json()["data"]
        by_id = {p["project_id"]: p for p in data["projects"]}
        # 실재만 노출, 유령 제외
        assert "2" in by_id and "Panthea" in by_id
        assert "AGI개발팀" not in by_id
        assert "HookOptCollect" not in by_id
        # 표시명 = 폴더명 (workspace_title '팀A'/'팀B' 아님)
        assert by_id["2"]["workspace_title"] == "2"
        assert by_id["Panthea"]["workspace_title"] == "Panthea"
    get_settings.cache_clear()
