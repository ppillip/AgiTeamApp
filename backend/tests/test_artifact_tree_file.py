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
    assert children["notes.txt"]["renderable"] is False  # unsupported 확장자
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
        svc.read_file("02.설계/notes.txt")
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
