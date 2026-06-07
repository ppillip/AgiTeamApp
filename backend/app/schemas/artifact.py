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
    render_mode: str          # markdown | pdf_stream | converted_preview | unsupported
    content_type: str
    encoding: str | None = None
    content: str | None = None
    stream_url: str | None = None
    converted_url: str | None = None
    download_allowed: bool = False
    sanitized: bool = False
    render_warnings: list[str] = []


ArtifactNode.model_rebuild()
