"""산출물 쓰기(WG-ART-05) 단위테스트: 서비스 경계 + POST /write 엔드포인트."""
from __future__ import annotations

import pytest

from app.errors import WebguiError


# --- 서비스 레벨 (보안 경계 = GET 계열과 동일 resolve()) -----------------------

def test_write_creates_new_md(svc, art_root):
    res = svc.write_file("02.설계/새문서.md", "# 새 문서\n본문")
    assert res["saved"] is True
    assert res["path"] == "02.설계/새문서.md"
    assert (art_root / "02.설계" / "새문서.md").read_text(encoding="utf-8") == "# 새 문서\n본문"


def test_write_overwrites_existing_md(svc, art_root):
    svc.write_file("01.분석/AN-20_요구사항정의서/doc.md", "수정됨")
    assert (art_root / "01.분석" / "AN-20_요구사항정의서" / "doc.md").read_text(encoding="utf-8") == "수정됨"


def test_write_creates_parent_dirs(svc, art_root):
    svc.write_file("03.신규/sub/note.md", "x")
    assert (art_root / "03.신규" / "sub" / "note.md").exists()


def test_write_rejects_non_md(svc):
    with pytest.raises(WebguiError) as exc:
        svc.write_file("02.설계/notes.txt", "x")
    assert exc.value.code == "invalid_artifact_type"
    assert exc.value.http_status == 400


def test_write_rejects_traversal(svc):
    with pytest.raises(WebguiError) as exc:
        svc.write_file("../escape.md", "x")
    assert exc.value.http_status == 403


def test_write_rejects_absolute_path(svc):
    with pytest.raises(WebguiError) as exc:
        svc.write_file("/etc/evil.md", "x")
    assert exc.value.code == "path_forbidden"


def test_write_rejects_secret(svc):
    with pytest.raises(WebguiError) as exc:
        svc.write_file("secret.key", "x")
    # secret 은 resolve 단계에서 artifact_hidden(403). (.md 검증 이전 차단)
    assert exc.value.http_status == 403


def test_write_rejects_directory(svc):
    # 02.설계 는 디렉토리지만 확장자가 없어 invalid_artifact_type(400) 으로 먼저 차단.
    with pytest.raises(WebguiError) as exc:
        svc.write_file("02.설계", "x")
    assert exc.value.http_status == 400


# --- 엔드포인트 레벨 -----------------------------------------------------------

def test_post_write_ok(client):
    r = client.post(
        "/api/webgui/artifacts/write",
        params={"project_id": "TestProj"},
        json={"path": "02.설계/api작성.md", "content": "# API\n저장"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["saved"] is True

    # 저장 후 GET /file 로 본문 회수 가능
    g = client.get(
        "/api/webgui/artifacts/file",
        params={"project_id": "TestProj", "path": "02.설계/api작성.md"},
    )
    assert g.status_code == 200
    assert "# API" in g.json()["data"]["file"]["content"]


def test_post_write_non_md_400(client):
    r = client.post(
        "/api/webgui/artifacts/write",
        params={"project_id": "TestProj"},
        json={"path": "02.설계/notes.txt", "content": "x"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_artifact_type"


def test_post_write_traversal_403(client):
    r = client.post(
        "/api/webgui/artifacts/write",
        params={"project_id": "TestProj"},
        json={"path": "../escape.md", "content": "x"},
    )
    assert r.status_code == 403
