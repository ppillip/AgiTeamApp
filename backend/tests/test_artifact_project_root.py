"""QI-WG-024(정밀화): 산출물 트리/뷰어 root 를 project_id 별로 해소.

규약(균일): 모든 프로젝트의 산출물 트리 루트 = `<project_root>/documents`.
- 트리 top 노드 이름 = "documents", 그 아래 00.standard·…·05.operation.
- UI 드롭다운 선택 project_id 를 따라 전환(AgiTeamApp 특례 없음).
보안: 해소된 per-project root 기준 allowlist/traversal 유지(타 프로젝트·상위 escape 차단).
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _client(monkeypatch, *, project_roots: dict):
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps(project_roots))
    monkeypatch.setenv("WEBGUI_PROJECT_ID", next(iter(project_roots)))
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    monkeypatch.delenv("WEBGUI_COLLECTOR_TOKEN", raising=False)
    monkeypatch.setenv("WEBGUI_ENABLE_BACKGROUND", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


def _scaffold_documents(project_root, marker_dir: str):
    docs = project_root / "documents"
    (docs / marker_dir).mkdir(parents=True, exist_ok=True)
    (docs / marker_dir / "doc.md").write_text("# 문서\n본문", encoding="utf-8")
    (docs / "README.md").write_text("# README", encoding="utf-8")


def test_tree_top_node_is_documents_per_project(tmp_path, monkeypatch):
    hooktest = tmp_path / "HookTest"
    _scaffold_documents(hooktest, "04.development")
    other = tmp_path / "Panthea2"
    _scaffold_documents(other, "05.operation")

    roots = {"HookTest": str(hooktest), "Panthea2": str(other)}
    with _client(monkeypatch, project_roots=roots) as c:
        # HookTest → top 노드 "documents", 04.development 보유
        r = c.get("/api/webgui/artifacts/tree", params={"project_id": "HookTest", "path": "", "depth": 1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["node"]["name"] == "documents"        # top 노드 이름
        assert data["root"] == "documents/"
        names = [n["name"] for n in data["node"]["children"]]
        assert "04.development" in names and "05.operation" not in names

        # 프로젝트 전환 → 트리 전환
        r2 = c.get("/api/webgui/artifacts/tree", params={"project_id": "Panthea2", "path": "", "depth": 1})
        assert r2.status_code == 200
        data2 = r2.json()["data"]
        assert data2["node"]["name"] == "documents"
        names2 = [n["name"] for n in data2["node"]["children"]]
        assert "05.operation" in names2 and "04.development" not in names2


def test_per_project_root_blocks_traversal(tmp_path, monkeypatch):
    hooktest = tmp_path / "HookTest"
    _scaffold_documents(hooktest, "04.development")
    secret = tmp_path / "Secret"
    _scaffold_documents(secret, "99.secret")

    roots = {"HookTest": str(hooktest), "Secret": str(secret)}
    with _client(monkeypatch, project_roots=roots) as c:
        # documents 루트에서 상위/타프로젝트로 escape 시도 → 차단
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "HookTest", "path": "../../Secret/documents"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "path_forbidden"
        # 절대경로 차단
        r2 = c.get("/api/webgui/artifacts/tree", params={"project_id": "HookTest", "path": "/etc"})
        assert r2.status_code == 403


def test_missing_documents_dir_graceful(tmp_path, monkeypatch):
    # documents 디렉터리가 없는 프로젝트 → 500 아닌 정의된 404 (graceful)
    empty = tmp_path / "NoDocs"
    empty.mkdir(parents=True, exist_ok=True)
    roots = {"NoDocs": str(empty)}
    with _client(monkeypatch, project_roots=roots) as c:
        r = c.get("/api/webgui/artifacts/tree", params={"project_id": "NoDocs", "path": ""})
        assert r.status_code == 404
        assert r.json()["ok"] is False
