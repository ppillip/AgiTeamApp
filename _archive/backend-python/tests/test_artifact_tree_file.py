"""산출물 트리/파일 읽기 단위테스트 (DV-20.3/.4, DS-40 §16~17)."""
from __future__ import annotations

import pytest

from app.errors import WebguiError


# --- 트리 (WG-ART-01) ------------------------------------------------------

def test_tree_root_lists_dirs_first(svc):
    data = svc.list_tree("", depth=1)
    children = data["node"]["children"]
    names = [c["name"] for c in children]
    # secret/숨김 제외
    assert ".env" not in names
    assert "secret.key" not in names
    # 디렉터리 우선
    dir_names = [c["name"] for c in children if c["node_type"] == "directory"]
    assert "01.분석" in dir_names and "02.설계" in dir_names


def test_tree_subdir_files_renderable_flag(svc):
    data = svc.list_tree("02.설계", depth=1)
    children = {c["name"]: c for c in data["node"]["children"]}
    assert children["sample.pdf"]["renderable"] is True
    assert children["sample.pdf"]["extension"] == "pdf"
    assert children["notes.txt"]["renderable"] is True   # .txt = code(코드뷰어 도입)
    assert children["unknown.bin"]["renderable"] is False  # 미지원 확장자
    assert children["DS-50_화면설계서"]["node_type"] == "directory"
    assert children["DS-50_화면설계서"]["has_children"] is True


def test_tree_recursive_depth(svc):
    data = svc.list_tree("01.분석", depth=2, recursive=True)
    sub = data["node"]["children"][0]
    assert sub["name"] == "AN-20_요구사항정의서"
    assert any(c["name"] == "doc.md" for c in sub.get("children", []))


def test_tree_on_file_is_not_directory(svc):
    with pytest.raises(WebguiError) as ei:
        svc.list_tree("02.설계/notes.txt", depth=1)
    assert ei.value.code == "not_directory"


def test_tree_invalid_depth(svc):
    with pytest.raises(WebguiError) as ei:
        svc.list_tree("", depth=0)
    assert ei.value.code == "invalid_tree_query"


# --- 파일 (WG-ART-02) ------------------------------------------------------

def test_read_markdown_inline(svc):
    r = svc.read_file("02.설계/DS-50_화면설계서/DS-50_화면설계서.md")
    assert r["status"] == 200
    assert r["file"]["render_mode"] == "markdown"
    assert "DS-50 화면설계서" in r["file"]["content"]


def test_read_markdown_sanitizes_xss(svc, art_root):
    p = art_root / "xss.md"
    p.write_text("# hi\n<script>alert('x')</script>\n[c](javascript:alert(1))", encoding="utf-8")
    r = svc.read_file("xss.md", sanitize=True)
    content = r["file"]["content"]
    assert "<script>" not in content
    assert "javascript:" not in content
    assert r["file"]["sanitized"] is True
    assert "raw_html_stripped" in r["file"]["render_warnings"]


def test_read_pdf_stream_mode(svc):
    r = svc.read_file("02.설계/sample.pdf")
    assert r["file"]["render_mode"] == "pdf_stream"
    assert r["file"]["stream_url"] is not None
    assert r["file"]["content"] is None


def test_read_docx_conversion_pending(svc):
    r = svc.read_file("02.설계/sample.docx")
    assert r["status"] == 202
    assert r["file"]["render_mode"] == "converted_preview"
    assert r["conversion"]["status"] == "pending"


def test_read_unsupported_format(svc):
    with pytest.raises(WebguiError) as ei:
        svc.read_file("02.설계/unknown.bin")   # .txt 는 이제 code 로 지원 → 미지원 표본 사용
    assert ei.value.code == "unsupported_media_type"


def test_read_svg_inline_mode(svc, art_root):
    # UI-07: SVG 뷰어 지원
    (art_root / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>',
        encoding="utf-8",
    )
    r = svc.read_file("diagram.svg")
    assert r["status"] == 200
    f = r["file"]
    assert f["render_mode"] == "image"
    assert f["mime_type"] == "image/svg+xml"
    assert f["content_type"] == "image/svg+xml"
    assert f["stream_url"] is not None          # <img src> 안전 렌더 경로
    assert "<svg" in (f["content"] or "")       # inline content 도 제공


def test_read_svg_sanitizes_script(svc, art_root):
    # SVG 내 script/on* 은 무력화(defense-in-depth)
    (art_root / "evil.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
        '<script>alert(2)</script><rect/></svg>',
        encoding="utf-8",
    )
    r = svc.read_file("evil.svg", sanitize=True)
    c = r["file"]["content"]
    assert "<script" not in c.lower()
    assert "onload=" not in c.lower()


def test_svg_signature_rejects_non_svg(svc, art_root):
    # 확장자만 .svg 이고 내용이 SVG/XML 이 아니면 거부
    (art_root / "fake.svg").write_text("just plain text not svg", encoding="utf-8")
    with pytest.raises(WebguiError) as ei:
        svc.read_file("fake.svg")
    assert ei.value.code == "unsupported_media_type"


def test_svg_tree_node_renderable(svc, art_root):
    (art_root / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    data = svc.list_tree("", depth=1)
    node = next(c for c in data["node"]["children"] if c["name"] == "pic.svg")
    assert node["renderable"] is True
    assert node["mime_type"] == "image/svg+xml"


def test_read_html_iframe_mode(svc, art_root):
    # UI-06: HTML 뷰어 지원 (FE 샌드박스 iframe)
    (art_root / "report.html").write_text(
        "<html><body><h1>리포트</h1></body></html>", encoding="utf-8")
    r = svc.read_file("report.html")
    assert r["status"] == 200
    f = r["file"]
    assert f["render_mode"] == "html"
    assert f["mime_type"] == "text/html"
    assert f["stream_url"] is not None       # iframe src 경로
    assert f["content"] is None              # raw HTML 본문은 inline 미제공(XSS 차단)


def test_read_htm_iframe_mode(svc, art_root):
    (art_root / "page.htm").write_text("<html></html>", encoding="utf-8")
    r = svc.read_file("page.htm")
    assert r["file"]["render_mode"] == "html"
    assert r["file"]["mime_type"] == "text/html"


def test_html_tree_node_renderable(svc, art_root):
    (art_root / "doc.html").write_text("<html></html>", encoding="utf-8")
    data = svc.list_tree("", depth=1)
    node = next(c for c in data["node"]["children"] if c["name"] == "doc.html")
    assert node["renderable"] is True
    assert node["mime_type"] == "text/html"


def test_read_directory_is_not_file(svc):
    with pytest.raises(WebguiError) as ei:
        svc.read_file("02.설계")
    assert ei.value.code == "not_file"


def test_read_too_large_markdown(svc, art_root):
    big = art_root / "big.md"
    big.write_text("x" * 5000, encoding="utf-8")
    with pytest.raises(WebguiError) as ei:
        svc.read_file("big.md", max_inline_bytes=1000)
    assert ei.value.code == "file_too_large"


def test_pdf_signature_mismatch_unsupported(svc, art_root):
    fake = art_root / "fake.pdf"
    fake.write_bytes(b"not a pdf")
    with pytest.raises(WebguiError) as ei:
        svc.read_file("fake.pdf")
    assert ei.value.code == "unsupported_media_type"


# --- 17-3: 래스터 이미지 (png/jpg/jpeg/gif/webp) -----------------------------

# 각 형식 최소 유효 매직 시그니처 바이트
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 8
_GIF = b"GIF89a" + b"\x00" * 10
_WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 8

_RASTER_CASES = [
    ("pic.png", _PNG, "image/png"),
    ("pic.jpg", _JPEG, "image/jpeg"),
    ("pic.jpeg", _JPEG, "image/jpeg"),
    ("pic.gif", _GIF, "image/gif"),
    ("pic.webp", _WEBP, "image/webp"),
]


@pytest.mark.parametrize("name,data,mime", _RASTER_CASES)
def test_read_raster_image_mode(svc, art_root, name, data, mime):
    (art_root / name).write_bytes(data)
    r = svc.read_file(name)
    f = r["file"]
    assert r["status"] == 200
    assert f["render_mode"] == "image"
    assert f["mime_type"] == mime
    assert f["content_type"] == mime
    assert f["stream_url"] is not None      # <img src=stream_url> 렌더 경로
    assert f["content"] is None             # 바이너리 — inline 본문 미제공
    assert f["encoding"] is None


@pytest.mark.parametrize("name,data,mime", _RASTER_CASES)
def test_raster_tree_node_renderable(svc, art_root, name, data, mime):
    (art_root / name).write_bytes(data)
    data_tree = svc.list_tree("", depth=1)
    node = next(c for c in data_tree["node"]["children"] if c["name"] == name)
    assert node["renderable"] is True
    assert node["mime_type"] == mime


@pytest.mark.parametrize("name", ["fake.png", "fake.jpg", "fake.jpeg", "fake.gif", "fake.webp"])
def test_raster_signature_rejects_forged_ext(svc, art_root, name):
    # 확장자만 이미지이고 실제 매직 시그니처가 아니면 거부 (위조 차단)
    (art_root / name).write_bytes(b"this is definitely not an image file")
    with pytest.raises(WebguiError) as ei:
        svc.read_file(name)
    assert ei.value.code == "unsupported_media_type"


def test_raster_webp_requires_webp_tag(svc, art_root):
    # RIFF 컨테이너지만 WEBP 태그가 아니면(예: WAV) 거부
    (art_root / "audio.webp").write_bytes(b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"\x00" * 8)
    with pytest.raises(WebguiError) as ei:
        svc.read_file("audio.webp")
    assert ei.value.code == "unsupported_media_type"


@pytest.mark.parametrize("name,data,mime", _RASTER_CASES)
def test_stream_raster_image_endpoint(client, art_root, name, data, mime):
    # 스트림 엔드포인트가 200 + 정확한 image/* content-type 으로 바이너리 응답
    (art_root / name).write_bytes(data)
    r = client.get("/api/webgui/artifacts/file/stream", params={"path": name})
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == mime
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.content == data


def test_stream_raster_traversal_blocked(client):
    # 경로 traversal 차단 (스트림 경로에도 동일 적용)
    r = client.get("/api/webgui/artifacts/file/stream", params={"path": "../../etc/passwd"})
    assert r.status_code == 403


def test_stream_forged_png_blocked(client, art_root):
    # 위조 시그니처는 스트림 단계에서도 거부 (open_stream → detect_format)
    (art_root / "evil.png").write_bytes(b"not a png at all")
    r = client.get("/api/webgui/artifacts/file/stream", params={"path": "evil.png"})
    assert r.status_code == 415


def test_svg_inline_regression_still_text(svc, art_root):
    # 회귀: svg 는 여전히 텍스트 inline content + image/svg+xml 로 동작
    (art_root / "diag.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>', encoding="utf-8"
    )
    f = svc.read_file("diag.svg")["file"]
    assert f["render_mode"] == "image"
    assert f["content_type"] == "image/svg+xml"
    assert "<svg" in (f["content"] or "")
    assert f["encoding"] == "utf-8"
