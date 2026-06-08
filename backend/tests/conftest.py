from __future__ import annotations

import os
from pathlib import Path

import pytest


def _build_sample_tree(root: Path) -> None:
    (root / "01.분석" / "AN-20_요구사항정의서").mkdir(parents=True, exist_ok=True)
    (root / "01.분석" / "AN-20_요구사항정의서" / "doc.md").write_text("# 요구사항\n본문", encoding="utf-8")

    design = root / "02.설계" / "DS-50_화면설계서"
    design.mkdir(parents=True, exist_ok=True)
    (design / "DS-50_화면설계서.md").write_text("# DS-50 화면설계서\n\n내용", encoding="utf-8")

    (root / "02.설계" / "sample.pdf").write_bytes(b"%PDF-1.7\n%fake pdf body\n")
    (root / "02.설계" / "sample.docx").write_bytes(b"PK\x03\x04fake-docx")
    (root / "02.설계" / "notes.txt").write_text("plain text", encoding="utf-8")

    # secret/숨김 후보
    (root / ".env").write_text("WEBGUI_API_TOKEN=supersecret", encoding="utf-8")
    (root / "secret.key").write_text("PRIVATE", encoding="utf-8")


@pytest.fixture
def art_root(tmp_path_factory) -> Path:
    # QI-WG-024 정밀화: 산출물 루트 = <project_root>/documents. 샘플 트리를 documents/ 하위에 둔다.
    proj = tmp_path_factory.mktemp("proj")
    docs = proj / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    _build_sample_tree(docs)
    return docs


@pytest.fixture
def svc(art_root):
    from app.services.artifact_service import ArtifactService

    return ArtifactService(art_root)


@pytest.fixture
def client(art_root, monkeypatch):
    from fastapi.testclient import TestClient

    import json

    monkeypatch.setenv("WEBGUI_ARTIFACTS_ROOT", str(art_root))
    # QI-WG-024 정밀화: 산출물 루트 = <project_root>/documents (균일). art_root = <proj>/documents
    # 이므로 project_root("TestProj") 를 art_root.parent 로 매핑하면 /documents == art_root.
    monkeypatch.setenv("WEBGUI_PROJECT_ID", "TestProj")
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps({"TestProj": str(art_root.parent)}))
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    monkeypatch.delenv("WEBGUI_COLLECTOR_TOKEN", raising=False)
    monkeypatch.setenv("WEBGUI_ENABLE_BACKGROUND", "false")  # 테스트에선 cmux 폴링 끔

    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
