"""산출물 컨텍스트 메뉴 신규 API (WG-ART-08/09/03D, DS-132) 단위테스트.

설계서 §9 parity fixture 후보 11건을 서비스 경계 + 엔드포인트 레벨로 커버한다.
Rust(backend-rs) 대조 기준 케이스이기도 하다(동등성 §9).
"""
from __future__ import annotations

import io
import unicodedata

import pytest

from app.errors import WebguiError


def _nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# =========================================================================
# WG-ART-08 새파일 생성 (서비스 경계)
# =========================================================================

def test_create_new_md(svc, art_root):
    """create 새문서.md in documents → full file + tree_refresh."""
    res = svc.create_file("02.설계", "새문서.md", template="empty", if_exists="error", root_type="documents")
    assert res["file"]["path"] == "02.설계/새문서.md"
    assert res["file"]["name"] == "새문서.md"
    assert res["file"]["extension"] == "md"
    assert res["file"]["render_mode"] == "markdown"
    assert res["file"]["content"] == ""
    # tree_refresh 힌트
    assert res["tree_refresh"] == {
        "root_type": "documents",
        "parent_path": "02.설계",
        "changed_path": "02.설계/새문서.md",
        "change_type": "created",
    }
    assert (art_root / "02.설계" / "새문서.md").exists()


def test_create_markdown_basic_template(svc, art_root):
    res = svc.create_file("02.설계", "노트.md", template="markdown_basic", root_type="documents")
    assert res["file"]["content"] == "# 노트\n"


def test_create_json_object_template(svc, art_root):
    res = svc.create_file("02.설계", "data.json", template="json_object", root_type="documents")
    assert (art_root / "02.설계" / "data.json").read_text(encoding="utf-8") == "{}\n"


def test_create_json_template_on_non_json_422(svc):
    with pytest.raises(WebguiError) as exc:
        svc.create_file("02.설계", "x.md", template="json_object")
    assert exc.value.code == "invalid_artifact_template"
    assert exc.value.http_status == 422


def test_create_duplicate_error_409(svc):
    """create duplicate if_exists=error → 409 artifact_already_exists."""
    svc.create_file("02.설계", "dup.md", if_exists="error")
    with pytest.raises(WebguiError) as exc:
        svc.create_file("02.설계", "dup.md", if_exists="error")
    assert exc.value.code == "artifact_already_exists"
    assert exc.value.http_status == 409


def test_create_duplicate_rename(svc, art_root):
    """create duplicate if_exists=rename → 'name (1).md'."""
    svc.create_file("02.설계", "dup.md", if_exists="error")
    res = svc.create_file("02.설계", "dup.md", if_exists="rename")
    assert res["file"]["name"] == "dup (1).md"
    assert (art_root / "02.설계" / "dup (1).md").exists()


def test_create_traversal_403(svc):
    """create ../evil.md → 403 path_forbidden."""
    with pytest.raises(WebguiError) as exc:
        svc.create_file("..", "evil.md")
    assert exc.value.code == "path_forbidden"


def test_create_traversal_in_filename_rejected(svc):
    with pytest.raises(WebguiError) as exc:
        svc.create_file("02.설계", "../evil.md")
    assert exc.value.code == "invalid_path"


def test_create_dotenv_hidden_403(svc):
    """create .env → 403 artifact_hidden."""
    with pytest.raises(WebguiError) as exc:
        svc.create_file("02.설계", ".env")
    assert exc.value.code == "artifact_hidden"


def test_create_html_rejected_415(svc):
    """새파일 html/htm 생성 기본 금지(§4.3)."""
    with pytest.raises(WebguiError) as exc:
        svc.create_file("02.설계", "page.html")
    assert exc.value.code == "unsupported_media_type"


def test_create_parent_is_file_422(svc):
    with pytest.raises(WebguiError) as exc:
        svc.create_file("02.설계/notes.txt", "x.md")
    assert exc.value.code == "not_directory"


def test_create_parent_missing_404(svc):
    with pytest.raises(WebguiError) as exc:
        svc.create_file("없는폴더", "x.md")
    assert exc.value.code == "artifact_path_not_found"


# =========================================================================
# WG-ART-09 파일 업로드 (서비스 경계)
# =========================================================================

def test_upload_png_valid_signature(svc, art_root):
    """upload diagram.png valid signature → render_mode=image."""
    res = svc.upload_file("02.설계", "diagram.png", PNG_1x1, if_exists="rename", root_type="documents")
    assert res["file"]["render_mode"] == "image"
    assert res["file"]["extension"] == "png"
    assert res["file"]["mime_type"] == "image/png"
    assert res["file"]["stream_url"]
    assert res["upload"]["sha256"]
    assert res["upload"]["size_bytes"] == len(PNG_1x1)
    assert res["tree_refresh"]["changed_path"] == "02.설계/diagram.png"
    assert (art_root / "02.설계" / "diagram.png").read_bytes() == PNG_1x1


def test_upload_fake_png_text_415(svc):
    """upload fake .png text body → 415 unsupported_media_type."""
    with pytest.raises(WebguiError) as exc:
        svc.upload_file("02.설계", "fake.png", b"this is not a png", root_type="documents")
    assert exc.value.code == "unsupported_media_type"
    assert exc.value.http_status == 415


def test_upload_too_large_413(svc):
    """upload 26 MiB → 413 file_too_large."""
    big = b"a" * (1024 + 1)
    with pytest.raises(WebguiError) as exc:
        svc.upload_file("02.설계", "big.txt", big, root_type="documents", max_upload_bytes=1024)
    assert exc.value.code == "file_too_large"
    assert exc.value.http_status == 413


def test_upload_binary_to_system_415(svc, art_root):
    """upload binary to system → 415 unsupported_media_type (텍스트/코드만 허용)."""
    # system 루트용 서비스로 직접 검증: root_type='system' 정책상 png 거절.
    from app.services.artifact_service import ArtifactService

    sys_root = art_root.parent / "system"
    (sys_root / "sub").mkdir(parents=True, exist_ok=True)
    sys_svc = ArtifactService(sys_root)
    with pytest.raises(WebguiError) as exc:
        sys_svc.upload_file("sub", "diagram.png", PNG_1x1, root_type="system")
    assert exc.value.code == "unsupported_media_type"


def test_upload_text_to_system_ok(svc, art_root):
    from app.services.artifact_service import ArtifactService

    sys_root = art_root.parent / "system"
    sys_root.mkdir(parents=True, exist_ok=True)
    sys_svc = ArtifactService(sys_root)
    res = sys_svc.upload_file("", "note.py", b"print('hi')\n", root_type="system")
    assert res["file"]["render_mode"] == "code"
    assert (sys_root / "note.py").exists()


def test_upload_invalid_utf8_text_422(svc):
    with pytest.raises(WebguiError) as exc:
        svc.upload_file("02.설계", "bad.txt", b"\xff\xfe\x00bad", root_type="documents")
    assert exc.value.code == "invalid_text_encoding"


def test_upload_duplicate_rename(svc, art_root):
    svc.upload_file("02.설계", "diagram.png", PNG_1x1, if_exists="rename")
    res = svc.upload_file("02.설계", "diagram.png", PNG_1x1, if_exists="rename")
    assert res["file"]["name"] == "diagram (1).png"


def test_upload_duplicate_error_409(svc):
    svc.upload_file("02.설계", "diagram.png", PNG_1x1, if_exists="error")
    with pytest.raises(WebguiError) as exc:
        svc.upload_file("02.설계", "diagram.png", PNG_1x1, if_exists="error")
    assert exc.value.code == "artifact_already_exists"


# =========================================================================
# WG-ART-03D 다운로드 (엔드포인트)
# =========================================================================

def test_download_korean_filename_disposition(client, art_root):
    """download 한글 문서.md → 200 + Content-Disposition with filename*."""
    (art_root / "한글 문서.md").write_text("# 본문\n", encoding="utf-8")
    r = client.get(
        "/api/webgui/artifacts/file/stream",
        params={"project_id": "TestProj", "path": "한글 문서.md", "download": "1"},
    )
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment")
    assert "filename*=UTF-8''" in cd
    assert "%ED%95%9C" in cd  # '한' percent-encoded


def test_download_directory_422(client):
    """download directory → 422 not_file."""
    r = client.get(
        "/api/webgui/artifacts/file/stream",
        params={"project_id": "TestProj", "path": "02.설계", "download": "1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "not_file"


def test_stream_without_download_no_disposition(client, art_root):
    (art_root / "02.설계" / "plain.pdf").write_bytes(b"%PDF-1.7\nx")
    r = client.get(
        "/api/webgui/artifacts/file/stream",
        params={"project_id": "TestProj", "path": "02.설계/plain.pdf"},
    )
    assert r.status_code == 200
    assert "content-disposition" not in {k.lower() for k in r.headers}


def test_download_filename_override_ext_mismatch_400(client, art_root):
    (art_root / "02.설계" / "doc.pdf").write_bytes(b"%PDF-1.7\nx")
    r = client.get(
        "/api/webgui/artifacts/file/stream",
        params={"project_id": "TestProj", "path": "02.설계/doc.pdf",
                "download": "1", "filename": "evil.exe"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_path"


# =========================================================================
# 엔드포인트 레벨 (create-file / upload)
# =========================================================================

def test_post_create_file_201(client, art_root):
    r = client.post(
        "/api/webgui/artifacts/create-file",
        json={"project_id": "TestProj", "parent_path": "02.설계",
              "filename": "신규문서.md", "template": "markdown_basic"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["file"]["name"] == "신규문서.md"
    assert body["data"]["tree_refresh"]["change_type"] == "created"


def test_post_create_duplicate_409(client):
    client.post("/api/webgui/artifacts/create-file",
                json={"project_id": "TestProj", "parent_path": "02.설계", "filename": "c.md"})
    r = client.post("/api/webgui/artifacts/create-file",
                    json={"project_id": "TestProj", "parent_path": "02.설계", "filename": "c.md"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "artifact_already_exists"


def test_post_upload_png_201(client, art_root):
    r = client.post(
        "/api/webgui/artifacts/upload",
        data={"project_id": "TestProj", "parent_path": "02.설계", "if_exists": "rename"},
        files={"file": ("diagram.png", io.BytesIO(PNG_1x1), "image/png")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["data"]["file"]["render_mode"] == "image"
    assert body["data"]["upload"]["filename"] == "diagram.png"


def test_post_upload_fake_png_415(client):
    r = client.post(
        "/api/webgui/artifacts/upload",
        data={"project_id": "TestProj", "parent_path": "02.설계"},
        files={"file": ("fake.png", io.BytesIO(b"not a png"), "image/png")},
    )
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_media_type"


def test_post_upload_traversal_403(client):
    r = client.post(
        "/api/webgui/artifacts/upload",
        data={"project_id": "TestProj", "parent_path": ".."},
        files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert r.status_code == 403


# =========================================================================
# QI-WG-048: NFD 한글 파일명 NFC 정규화 + 빈 파일 다운로드 (Python↔Rust parity)
# =========================================================================

def test_create_nfd_korean_normalized_to_nfc(svc, art_root):
    """B2: create NFD 한글 → 응답명/저장명 NFC."""
    out = svc.create_file("02.설계", _nfd("한글파일.md"), template="empty", root_type="documents")
    assert out["file"]["name"] == _nfc("한글파일.md") == "한글파일.md"
    assert out["file"]["path"] == "02.설계/한글파일.md"
    assert (art_root / "02.설계" / "한글파일.md").exists()


def test_create_nfd_duplicate_rename_normalized(svc):
    out1 = svc.create_file("02.설계", _nfd("한글파일.md"), if_exists="error")
    assert out1["file"]["name"] == "한글파일.md"
    out2 = svc.create_file("02.설계", _nfd("한글파일.md"), if_exists="rename")
    assert out2["file"]["name"] == "한글파일 (1).md"


def test_upload_nfd_korean_normalized_to_nfc(svc, art_root):
    """B4: upload NFD 한글 → upload.filename / file.name NFC."""
    out = svc.upload_file("02.설계", _nfd("업로드한글.txt"), b"hi\n", root_type="documents")
    assert out["upload"]["filename"] == _nfc("업로드한글.txt") == "업로드한글.txt"
    assert out["file"]["name"] == "업로드한글.txt"
    assert (art_root / "02.설계" / "업로드한글.txt").exists()


def test_resolve_nfd_path_matches_nfc_stored(svc):
    svc.create_file("02.설계", _nfd("문서.md"), if_exists="error")
    res = svc.read_file("02.설계/" + _nfd("문서.md"), root_type="documents")
    assert res["file"]["name"] == "문서.md"


def test_validate_filename_nfc(svc):
    assert svc.validate_filename(_nfd("한글.md")) == _nfc("한글.md")


def test_download_empty_file_200(client, art_root):
    """B5 회귀: 0바이트 파일 다운로드 → 200, Content-Length 0 (Rust 패닉 대조용)."""
    (art_root / "02.설계" / "empty.md").write_text("", encoding="utf-8")
    r = client.get(
        "/api/webgui/artifacts/file/stream",
        params={"project_id": "TestProj", "path": "02.설계/empty.md", "download": "1"},
    )
    assert r.status_code == 200
    assert r.headers["content-length"] == "0"
    assert r.content == b""
    assert r.headers["content-disposition"].startswith("attachment")


def test_download_nfd_path_empty_korean(client, art_root):
    """B5: NFD 한글 빈 파일 생성 후 NFD path 다운로드 → 200 + filename* NFC."""
    r0 = client.post(
        "/api/webgui/artifacts/create-file",
        json={"project_id": "TestProj", "parent_path": "02.설계", "filename": _nfd("한글파일.md")},
    )
    assert r0.status_code == 201
    r = client.get(
        "/api/webgui/artifacts/file/stream",
        params={"project_id": "TestProj", "path": "02.설계/" + _nfd("한글파일.md"), "download": "1"},
    )
    assert r.status_code == 200
    assert r.headers["content-length"] == "0"
    cd = r.headers["content-disposition"]
    # filename* 는 NFC 한글로 디코딩되어야 한다.
    from urllib.parse import unquote
    import re
    m = re.search(r"filename\*=UTF-8''(\S+)", cd)
    assert m and unquote(m.group(1)) == _nfc("한글파일.md")
