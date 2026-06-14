"""코드탭(제우스 2026-06-14): 산출물 트리 root_type {documents, system} 전환.

계약:
- root_type 미지정/빈값 = documents (하위호환).
- root_type=documents → <project_root>/documents (현행 유지).
- root_type=system → <project_root>/system.
- system 루트도 documents 와 동일한 allowlist/traversal/symlink-escape 안전성.
- 미지의 root_type → 400 invalid_request.
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


def _scaffold(project_root):
    docs = project_root / "documents" / "04.development"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "doc.md").write_text("# 문서\n본문", encoding="utf-8")
    sysd = project_root / "system" / "backend"
    sysd.mkdir(parents=True, exist_ok=True)
    (sysd / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (project_root / "system" / "NOTE.md").write_text("# 소스 노트", encoding="utf-8")
    # brain(persona 탭) — 역할 폴더 + persona.md
    for role in ("PM", "DeveloperBE", "QA"):
        rd = project_root / "brain" / role
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "persona.md").write_text(f"# {role} 페르소나", encoding="utf-8")
    (project_root / ".agiteam").mkdir(parents=True, exist_ok=True)  # is_project_dir 마커


def test_default_root_type_is_documents(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        # root_type 미지정 → documents (하위호환)
        r = c.get("/api/webgui/artifacts/tree", params={"project_id": "Proj", "path": "", "depth": 1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["node"]["name"] == "documents"
        assert data["root"] == "documents/"
        names = [n["name"] for n in data["node"]["children"]]
        assert "04.development" in names
        # 빈값도 documents
        r2 = c.get("/api/webgui/artifacts/tree", params={"project_id": "Proj", "root_type": "", "path": ""})
        assert r2.status_code == 200
        assert r2.json()["data"]["root"] == "documents/"


def test_root_type_system_tree_and_file(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        # system 트리 top 노드 = "system", backend 보유
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "Proj", "root_type": "system", "path": "", "depth": 1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["node"]["name"] == "system"
        assert data["root"] == "system/"
        names = [n["name"] for n in data["node"]["children"]]
        assert "backend" in names
        # system 내 .md 파일 조회 → stream_url 에 root_type=system 부착
        rf = c.get("/api/webgui/artifacts/file",
                   params={"project_id": "Proj", "root_type": "system", "path": "NOTE.md"})
        assert rf.status_code == 200, rf.text
        f = rf.json()["data"]["file"]
        assert f["render_mode"] == "markdown"
        assert "소스 노트" in f["content"]


def test_root_type_documents_explicit(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "Proj", "root_type": "documents", "path": ""})
        assert r.status_code == 200
        assert r.json()["data"]["root"] == "documents/"


def test_system_root_blocks_traversal_to_documents(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        # system 루트에서 상위로 escape → documents 접근 시도 차단
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "Proj", "root_type": "system", "path": "../documents"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "path_forbidden"
        # 절대경로 차단
        r2 = c.get("/api/webgui/artifacts/tree",
                   params={"project_id": "Proj", "root_type": "system", "path": "/etc"})
        assert r2.status_code == 403


def test_root_type_persona_tree_and_file(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        # persona 트리 top 노드 = "brain", 역할 폴더 보유
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "Proj", "root_type": "persona", "path": "", "depth": 1})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["node"]["name"] == "brain"
        assert data["root"] == "brain/"
        names = [n["name"] for n in data["node"]["children"]]
        assert "PM" in names and "DeveloperBE" in names and "QA" in names
        # brain 내 persona.md 조회
        rf = c.get("/api/webgui/artifacts/file",
                   params={"project_id": "Proj", "root_type": "persona", "path": "PM/persona.md"})
        assert rf.status_code == 200, rf.text
        f = rf.json()["data"]["file"]
        assert f["render_mode"] == "markdown"
        assert "PM 페르소나" in f["content"]


def test_persona_root_blocks_traversal(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "Proj", "root_type": "persona", "path": "../system"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "path_forbidden"


def test_invalid_root_type_rejected(tmp_path, monkeypatch):
    pr = tmp_path / "Proj"
    _scaffold(pr)
    with _client(monkeypatch, project_roots={"Proj": str(pr)}) as c:
        r = c.get("/api/webgui/artifacts/tree",
                  params={"project_id": "Proj", "root_type": "etc", "path": ""})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "invalid_request"
