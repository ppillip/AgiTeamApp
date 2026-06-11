"""WG-MSG-06 이미지 업로드/preview 엔드포인트 테스트 (DV-90 / DS-40 §7.6)."""
from __future__ import annotations

import struct
import zlib

import pytest
from fastapi.testclient import TestClient


def _png(w: int = 16, h: int = 16) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


@pytest.fixture
def client(tmp_path, monkeypatch):
    proj = tmp_path / "UpProj"
    (proj / ".agiteam").mkdir(parents=True)
    import json

    monkeypatch.setenv("WEBGUI_PROJECT_ID", "UpProj")
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps({"UpProj": str(proj)}))
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    monkeypatch.delenv("WEBGUI_COLLECTOR_TOKEN", raising=False)
    monkeypatch.setenv("WEBGUI_ENABLE_BACKGROUND", "false")
    monkeypatch.setenv("WEBGUI_ARTIFACT_WATCHER_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


_URL = "/api/webgui/message-attachments/images"


def test_upload_success(client):
    r = client.post(
        _URL,
        data={"project_id": "UpProj", "client_attachment_id": "c1"},
        files={"file": ("paste.png", _png(40, 20), "image/png")},
    )
    assert r.status_code == 201
    att = r.json()["data"]["attachment"]
    assert att["attachment_id"].startswith("att_")
    assert att["client_attachment_id"] == "c1"
    assert att["mime_type"] == "image/png"
    assert (att["width"], att["height"]) == (40, 20)
    assert att["preview_url"].endswith("/preview")
    # 절대경로 미노출
    assert "abs_path" not in att
    assert "/Users/" not in str(att) and "/tmp" not in str(att).lower()


def test_upload_rejects_non_image(client):
    r = client.post(
        _URL,
        data={"project_id": "UpProj"},
        files={"file": ("x.png", b"not an image", "image/png")},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_image"


def test_upload_rejects_unknown_project(client):
    r = client.post(
        _URL,
        data={"project_id": "GhostProj"},
        files={"file": ("x.png", _png(), "image/png")},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "project_not_found"


def test_upload_missing_project_id(client):
    r = client.post(_URL, files={"file": ("x.png", _png(), "image/png")})
    # project_id form 누락 → 전역 검증 핸들러(400 invalid_request)
    assert r.status_code == 400


def test_preview_self_contained_no_project_id(client):
    """DS-40 §7.6.7: preview 는 project_id query 없이 attachment_id 만으로 200."""
    up = client.post(
        _URL,
        data={"project_id": "UpProj"},
        files={"file": ("paste.png", _png(8, 8), "image/png")},
    )
    aid = up.json()["data"]["attachment"]["attachment_id"]
    # preview_url 도 self-contained (project_id query 없음)
    assert up.json()["data"]["attachment"]["preview_url"] == f"/api/webgui/message-attachments/{aid}/preview"
    # project_id query 전혀 없이 호출 → 200
    pv = client.get(f"/api/webgui/message-attachments/{aid}/preview")
    assert pv.status_code == 200
    assert pv.headers["content-type"].startswith("image/png")
    assert pv.content[:8] == b"\x89PNG\r\n\x1a\n"  # 원본 바이트 반환
    # sidecar project 해소가 동작: project_id query 를 붙여도(무시) 동일 200 (하위호환)
    pv2 = client.get(f"/api/webgui/message-attachments/{aid}/preview", params={"project_id": "ignored"})
    assert pv2.status_code == 200


def test_preview_invalid_attachment_id_404(client):
    # 형식 불일치/미등록 ID 는 404 attachment_not_found 로 은닉
    for bad in ["att_zzzz", "not_att", "att_" + "0" * 32]:
        pv = client.get(f"/api/webgui/message-attachments/{bad}/preview")
        assert pv.status_code == 404
        assert pv.json()["error"]["code"] == "attachment_not_found"


def test_preview_expired_410(client, monkeypatch):
    # TTL 0 으로 업로드 후 preview → 410 attachment_expired
    monkeypatch.setenv("WEBGUI_ATTACHMENT_TTL_SECONDS", "-1")
    from app.config import get_settings

    get_settings.cache_clear()
    up = client.post(
        _URL, data={"project_id": "UpProj"}, files={"file": ("p.png", _png(), "image/png")}
    )
    aid = up.json()["data"]["attachment"]["attachment_id"]
    pv = client.get(f"/api/webgui/message-attachments/{aid}/preview")
    assert pv.status_code == 410
    assert pv.json()["error"]["code"] == "attachment_expired"
    get_settings.cache_clear()
