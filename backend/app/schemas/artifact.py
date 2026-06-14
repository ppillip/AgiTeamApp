"""산출물 스키마 (DS-40 §4.6, §4.7)."""
from __future__ import annotations

from pydantic import BaseModel


class ArtifactNode(BaseModel):
    path: str
    name: str
    node_type: str            # directory | file
    extension: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    has_children: bool = False
    renderable: bool = False
    children: list["ArtifactNode"] | None = None


class ArtifactFile(BaseModel):
    path: str
    name: str
    extension: str
    mime_type: str
    size_bytes: int
    render_mode: str          # markdown | code | pdf_stream | image | html | converted_preview
    content_type: str
    encoding: str | None = None
    content: str | None = None
    stream_url: str | None = None
    converted_url: str | None = None
    download_allowed: bool = False
    sanitized: bool = False
    render_warnings: list[str] = []
    language_hint: str | None = None   # code 모드 CodeMirror 언어 ID (json·python·bash·...)


class ArtifactWriteRequest(BaseModel):
    path: str
    content: str
    # 코드/페르소나 탭 저장 루트 선택 (결함수정 2026-06-14).
    # FE(writeFile)는 project_id·root_type 을 POST body 로 보낸다(path/content 와 같은 위치).
    # 이전엔 BE write 가 이 둘을 Query 로만 읽어 body 값이 무시 → root_type 이 documents 로
    # 기본화되어 brain/system 편집이 documents 에 저장되던 버그. read(GET)는 query 유지.
    project_id: str | None = None
    root_type: str | None = None


ArtifactNode.model_rebuild()
