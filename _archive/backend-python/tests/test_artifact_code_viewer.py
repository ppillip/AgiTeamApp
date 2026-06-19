"""코드뷰어 (코드탭, 제우스 2026-06-14, 아테나 설계): render_mode='code' + language_hint + write.

계약(FE 이리스): GET /artifacts/file → {render_mode:'code', content:<text>, language_hint:<id>, extension}.
write 는 코드 확장자 허용(secret/traversal/크기제한은 read 와 동일 유지).
"""
from __future__ import annotations

import pytest

from app.errors import WebguiError
from app.services.artifact_service import ArtifactService, _CODE_LANG, _RENDER_MODE


# --- 서비스 레벨: read_file render_mode='code' + language_hint ---

@pytest.mark.parametrize("fname,content,lang", [
    ("config.json", '{"a": 1}', "json"),
    ("app.py", "print('hi')\n", "python"),
    ("deploy.sh", "#!/bin/bash\necho hi\n", "bash"),
    ("conf.yaml", "a: 1\n", "yaml"),
    ("main.rs", "fn main() {}\n", "rust"),
    ("q.sql", "select 1;\n", "sql"),
    ("style.css", "a{color:red}\n", "css"),
    ("data.xml", "<root/>\n", "xml"),
    ("notes.txt", "plain\n", "text"),
])
def test_code_file_read(tmp_path, fname, content, lang):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / fname).write_text(content, encoding="utf-8")
    svc = ArtifactService(docs)
    res = svc.read_file(fname)
    f = res["file"]
    assert f["render_mode"] == "code"
    assert f["content"] == content
    assert f["language_hint"] == lang
    assert f["extension"] == fname.rsplit(".", 1)[1]
    assert res["status"] == 200


def test_code_extensionless_dockerfile(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "Dockerfile").write_text("FROM python:3.14\n", encoding="utf-8")
    svc = ArtifactService(docs)
    f = svc.read_file("Dockerfile")["file"]
    assert f["render_mode"] == "code"
    assert f["language_hint"] == "dockerfile"
    assert "FROM python" in f["content"]


def test_markdown_stays_markdown_not_code(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "doc.md").write_text("# 제목\n본문", encoding="utf-8")
    svc = ArtifactService(docs)
    f = svc.read_file("doc.md")["file"]
    assert f["render_mode"] == "markdown"   # 코드모드 아님(현행 유지)
    assert f["language_hint"] is None


def test_html_stays_html_not_code(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "page.html").write_text("<html></html>", encoding="utf-8")
    svc = ArtifactService(docs)
    f = svc.read_file("page.html")["file"]
    assert f["render_mode"] == "html"       # 코드모드 아님(현행 유지)


def test_code_file_too_large(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "big.py").write_text("x = 1\n" * 10, encoding="utf-8")
    svc = ArtifactService(docs)
    with pytest.raises(WebguiError) as exc:
        svc.read_file("big.py", max_inline_bytes=10)   # 작은 한계로 강제 초과
    assert exc.value.code == "file_too_large"


def test_code_invalid_utf8_replaced(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "bin.py").write_bytes(b"x = '\xff\xfe'\n")   # 깨진 바이트
    svc = ArtifactService(docs)
    f = svc.read_file("bin.py")["file"]
    assert f["render_mode"] == "code"
    assert "�" in f["content"]   # errors=replace → U+FFFD


def test_render_mode_table_excludes_md_html():
    # 코드 확장자는 'code', md/html 은 보존(회귀 가드)
    assert _RENDER_MODE["py"] == "code" and _RENDER_MODE["json"] == "code"
    assert _RENDER_MODE["md"] == "markdown" and _RENDER_MODE["markdown"] == "markdown"
    assert _RENDER_MODE["html"] == "html" and _RENDER_MODE["htm"] == "html"
    assert "md" not in _CODE_LANG and "html" not in _CODE_LANG


# --- write: 코드 확장자 허용 + 보안 유지 ---

def test_write_code_ext_allowed(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    svc = ArtifactService(docs)
    for fname, body in [("a.py", "x=1"), ("b.json", "{}"), ("c.yaml", "a: 1"), ("d.sh", "echo")]:
        res = svc.write_file(fname, body)
        assert res["saved"] is True
        assert (docs / fname).read_text(encoding="utf-8") == body


def test_write_rejects_unlisted_ext(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    svc = ArtifactService(docs)
    with pytest.raises(WebguiError) as exc:
        svc.write_file("evil.exe", "x")
    assert exc.value.code == "invalid_artifact_type"


def test_write_code_still_blocks_secret(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    svc = ArtifactService(docs)
    # .env 는 secret → resolve()에서 403(artifact_hidden), allowlist 무관
    with pytest.raises(WebguiError) as exc:
        svc.write_file(".env", "TOKEN=x")
    assert exc.value.http_status == 403


def test_write_code_still_blocks_traversal(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    svc = ArtifactService(docs)
    with pytest.raises(WebguiError) as exc:
        svc.write_file("../escape.py", "x")
    assert exc.value.http_status == 403
