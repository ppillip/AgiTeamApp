"""앱 기동/엔드포인트 응답 스모크 테스트 (구동확인).

DB 비의존 경로(healthz, openapi, 산출물 트리/파일)를 HTTP 로 검증한다.
"""
from __future__ import annotations


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ok"


def test_openapi(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/webgui/messages" in paths
    assert "/api/webgui/rooms" in paths
    assert "/api/webgui/artifacts/tree" in paths
    assert "/api/webgui/artifacts/file" in paths


def test_artifact_tree_endpoint(client):
    r = client.get("/api/webgui/artifacts/tree", params={"path": "", "depth": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    names = [c["name"] for c in body["data"]["node"]["children"]]
    assert ".env" not in names  # secret 숨김


def test_artifact_tree_traversal_blocked_endpoint(client):
    r = client.get("/api/webgui/artifacts/tree", params={"path": "../../../etc"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "path_forbidden"


def test_artifact_file_markdown_endpoint(client):
    r = client.get(
        "/api/webgui/artifacts/file",
        params={"path": "02.설계/DS-50_화면설계서/DS-50_화면설계서.md"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["file"]["render_mode"] == "markdown"


def test_artifact_file_unsupported_endpoint(client):
    r = client.get("/api/webgui/artifacts/file", params={"path": "02.설계/notes.txt"})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media_type"


def test_projects_endpoint_contract(client):
    # QI-WG-010: DS-40 ProjectSummary 계약 검증 (DB 불요)
    from app.services.cmux_discovery import registry
    from tests.test_discovery import SAMPLE_TREE

    registry.refresh_from_tree(SAMPLE_TREE)
    r = client.get("/api/webgui/projects")
    assert r.status_code == 200
    data = r.json()["data"]
    # 최상위 selected_project_id
    assert data["selected_project_id"] == "Panthea"
    by_id = {p["project_id"]: p for p in data["projects"]}
    assert "Panthea" in by_id
    p = by_id["Panthea"]
    # ProjectSummary 필수 필드
    for key in (
        "project_id",
        "workspace_title",
        "root_path",
        "connection_state",
        "pm_connection_state",
        "room_count",
        "last_discovered_at",
        "roles",
    ):
        assert key in p, f"missing {key}"
    assert p["workspace_title"] == "Panthea"
    assert p["root_path"].endswith("/Panthea")          # projects_base/<project_id>
    assert p["connection_state"] == "connected"
    assert p["pm_connection_state"] == "connected"
    assert p["room_count"] == 7
    # 역할 공개 키는 role (QI-WG-009)
    assert p["roles"][0]["role"] == "PM"
    assert "role_id" not in p["roles"][0]


def test_openapi_has_projects(client):
    r = client.get("/openapi.json")
    assert "/api/webgui/projects" in r.json()["paths"]


def test_db_endpoint_graceful_when_no_db(client):
    # PostgreSQL 미가동 환경에서 DB 의존 엔드포인트는 503 으로 안전 처리되어야 한다.
    r = client.get("/api/webgui/rooms")
    assert r.status_code in (503, 500)
    assert r.json()["ok"] is False
