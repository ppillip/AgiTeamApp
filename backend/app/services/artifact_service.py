"""산출물 서비스 (DV-20.3/.4).

설계: DS-20 §13 (접근 경계·렌더 경계), DS-40 §16~18, DS-60 §11.
보안 필수:
- allowlist 루트 = documents/products/AgiTeamApp/ (DS-20 §13.1)
- traversal/절대경로/symlink escape/숨김·secret 차단 (DS-20 §13.2, DS-40 §16.4)
- system/AgiTeamBuilder/ 접근 차단
- 응답/로그에 host 절대경로·파일본문(차단 시) 노출 금지

DB 미사용: 트리/메타/본문/렌더 입력은 매 요청 파일시스템에서 직접 읽는다 (DS-30 §10).
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .. import errors

# 지원 형식 (DS-40 §17.5)
_RENDER_MODE = {
    "md": "markdown",
    "markdown": "markdown",
    "pdf": "pdf_stream",
    "svg": "image",               # UI-07: SVG 이미지 표시 (FE ArtifactViewer 'image' 모드)
    "html": "html",               # UI-06: HTML 표시 (FE ArtifactViewer 'html' 샌드박스 iframe)
    "htm": "html",
    "pptx": "converted_preview",
    "docx": "converted_preview",
}
_MIME = {
    "md": "text/markdown",
    "markdown": "text/markdown",
    "pdf": "application/pdf",
    "svg": "image/svg+xml",
    "html": "text/html",
    "htm": "text/html",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# 숨김/secret 후보 (DS-20 §13.2, DS-40 §16.3). secret 은 include_hidden 여도 차단.
_SECRET_NAME = re.compile(
    r"(?i)(^\.env($|\.)|secret|credential|\.pem$|\.key$|(^|[._-])token($|[._-])|"
    r"settings\.local\.json$|id_rsa|\.p12$|\.pfx$)"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class ResolvedPath:
    abs_path: Path        # 검증 완료된 실제 경로
    rel_path: str         # allowlist 루트 기준 상대경로 (응답용)


def _ext(name: str) -> str | None:
    if "." not in name:
        return None
    return name.rsplit(".", 1)[1].lower()


class ArtifactService:
    def __init__(self, root: Path, display_root: str = "documents/products/AgiTeamApp/") -> None:
        # 루트 자체를 realpath 로 고정 (symlink 루트 대비)
        self.root = Path(root).resolve()
        # 응답용 논리 루트 라벨 (project_id 별로 다름, QI-WG-024). host 절대경로 비노출.
        self._display = display_root

    # --- 경로 정규화/검증 (보안 핵심) -------------------------------------

    def resolve(self, raw_path: str | None) -> ResolvedPath:
        """클라이언트 입력 경로를 allowlist 루트 기준으로 안전 해석.

        실패 시 적절한 WebguiError 를 raise 한다. host 절대경로는 노출하지 않는다.
        """
        path = raw_path or ""
        # 1) 형식 오류: null byte / 제어문자
        if _CONTROL_CHARS.search(path):
            raise errors.invalid_path()
        # 유니코드 정규화 (NFC) — 표시/비교 일관
        path = unicodedata.normalize("NFC", path)

        # 2) 절대경로 / drive prefix / UNC 차단 (DS-40 §16.4)
        # NOTE: DS-60 §11.2 는 "선행 / 제거" 를 명시하나 DS-40 §16.4 는 "절대 경로 요청 -> path_forbidden"
        # 을 명시한다. 보안 우선 원칙(제우스 지시)에 따라 절대 경로는 차단한다. (PM 보고 대상)
        if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
            raise errors.path_forbidden()
        # backslash 를 슬래시로 통일
        path = path.replace("\\", "/")
        # 절대 경로(선행 /) 차단
        if path.startswith("/"):
            raise errors.path_forbidden()

        # 3) 빈 경로 -> 루트
        if path == "" or path == ".":
            return ResolvedPath(abs_path=self.root, rel_path="")

        # 4) 세그먼트 검증: '..' traversal 차단
        segments = [s for s in path.split("/") if s not in ("", ".")]
        for seg in segments:
            if seg == "..":
                raise errors.path_forbidden()

        rel = "/".join(segments)
        candidate = (self.root / rel)

        # 5) 실제 경로 resolve 후 루트 내부인지 재검증 (symlink escape 차단)
        try:
            real = candidate.resolve()
        except (OSError, RuntimeError):
            raise errors.artifact_path_not_found()

        if not self._is_within_root(real):
            # symlink 가 루트 밖을 가리키면 symlink_forbidden, 그 외엔 path_forbidden
            if candidate.is_symlink() or self._has_symlink_component(candidate):
                raise errors.symlink_forbidden()
            raise errors.path_forbidden()

        # 6) 숨김/secret 차단 (마지막 세그먼트 + 중간 세그먼트 모두 점검)
        for seg in segments:
            if _SECRET_NAME.search(seg):
                raise errors.artifact_hidden()

        return ResolvedPath(abs_path=real, rel_path=rel)

    def _is_within_root(self, p: Path) -> bool:
        try:
            p.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _has_symlink_component(self, p: Path) -> bool:
        cur = p
        while True:
            if cur == self.root or cur == cur.parent:
                break
            if cur.is_symlink():
                return True
            cur = cur.parent
        return False

    def _is_hidden_or_secret(self, name: str, include_hidden: bool) -> bool:
        if _SECRET_NAME.search(name):
            return True  # secret 은 항상 차단
        if name.startswith(".") and not include_hidden:
            return True
        return False

    # --- 트리 (WG-ART-01 / DV-20.3) ---------------------------------------

    def list_tree(
        self,
        raw_path: str | None,
        *,
        depth: int = 1,
        recursive: bool = False,
        include_files: bool = True,
        include_hidden: bool = False,
        extensions: list[str] | None = None,
        max_nodes: int = 2000,
        max_depth: int = 6,
    ) -> dict:
        if depth < 1 or depth > max_depth:
            raise errors.invalid_tree_query(f"depth must be 1..{max_depth}")
        rp = self.resolve(raw_path)
        if not rp.abs_path.exists():
            raise errors.artifact_path_not_found()
        if not rp.abs_path.is_dir():
            raise errors.not_directory()

        counter = {"n": 0}
        node = self._build_node(rp.abs_path, rp.rel_path, is_dir=True)
        node["children"] = self._children(
            rp.abs_path,
            rp.rel_path,
            remaining_depth=depth,
            recursive=recursive,
            include_files=include_files,
            include_hidden=include_hidden,
            extensions=extensions,
            counter=counter,
            max_nodes=max_nodes,
        )
        return {"root": str(self._display_root()), "path": rp.rel_path, "node": node}

    def _display_root(self) -> str:
        # 응답에는 host 절대경로 대신 논리 루트 표기를 사용 (project_id 별 해소, QI-WG-024)
        return self._display

    def _children(
        self,
        abs_dir: Path,
        rel_dir: str,
        *,
        remaining_depth: int,
        recursive: bool,
        include_files: bool,
        include_hidden: bool,
        extensions: list[str] | None,
        counter: dict,
        max_nodes: int,
    ) -> list[dict]:
        if remaining_depth <= 0:
            return []
        try:
            entries = list(os.scandir(abs_dir))
        except OSError:
            return []
        # 디렉터리 우선, 이름 오름차순 (DS-40 §16.3)
        entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        out: list[dict] = []
        for e in entries:
            if counter["n"] >= max_nodes:
                break
            name = e.name
            if e.is_symlink():
                continue  # symlink 는 MVP 차단 (DS-40 §16.3)
            if self._is_hidden_or_secret(name, include_hidden):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if not is_dir and not include_files:
                continue
            ext = None if is_dir else _ext(name)
            if not is_dir and extensions and (ext not in extensions):
                continue
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            counter["n"] += 1
            node = self._build_node(Path(e.path), child_rel, is_dir=is_dir)
            if is_dir and recursive and remaining_depth - 1 > 0:
                node["children"] = self._children(
                    Path(e.path),
                    child_rel,
                    remaining_depth=remaining_depth - 1,
                    recursive=recursive,
                    include_files=include_files,
                    include_hidden=include_hidden,
                    extensions=extensions,
                    counter=counter,
                    max_nodes=max_nodes,
                )
            out.append(node)
        return out

    def _build_node(self, abs_path: Path, rel_path: str, *, is_dir: bool) -> dict:
        name = abs_path.name if rel_path else abs_path.name
        ext = None if is_dir else _ext(name)
        size = None
        has_children = False
        renderable = False
        if is_dir:
            try:
                has_children = any(True for _ in os.scandir(abs_path))
            except OSError:
                has_children = False
        else:
            try:
                size = abs_path.stat().st_size
            except OSError:
                size = None
            renderable = ext in _RENDER_MODE
        return {
            "path": rel_path,
            "name": name,
            "node_type": "directory" if is_dir else "file",
            "extension": ext,
            "mime_type": (None if is_dir else _MIME.get(ext or "")),
            "size_bytes": size,
            "has_children": has_children,
            "renderable": renderable,
        }

    # --- 파일 메타/내용 (WG-ART-02 / DV-20.4) -----------------------------

    def detect_format(self, abs_path: Path, ext: str | None) -> str | None:
        """확장자 + 파일 signature 로 형식 판정 (DS-40 §17.6)."""
        if ext in _RENDER_MODE:
            # PDF signature 보강
            if ext == "pdf":
                try:
                    with open(abs_path, "rb") as f:
                        if f.read(5) != b"%PDF-":
                            return None
                except OSError:
                    return None
            # docx/pptx 는 zip(PK) signature
            if ext in ("docx", "pptx"):
                try:
                    with open(abs_path, "rb") as f:
                        if f.read(2) != b"PK":
                            return None
                except OSError:
                    return None
            # svg 는 XML/<svg> signature (선두 BOM/공백 허용)
            if ext == "svg":
                try:
                    with open(abs_path, "rb") as f:
                        head = f.read(1024).lstrip(b"\xef\xbb\xbf").lstrip()
                    if not (head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in head):
                        return None
                except OSError:
                    return None
            return ext
        return None

    def read_file(
        self,
        raw_path: str,
        *,
        prefer: str = "inline",
        sanitize: bool = True,
        max_inline_bytes: int = 1_048_576,
    ) -> dict:
        rp = self.resolve(raw_path)
        if not rp.abs_path.exists():
            raise errors.artifact_path_not_found()
        if rp.abs_path.is_dir():
            raise errors.not_file()

        name = rp.abs_path.name
        ext = _ext(name)
        fmt = self.detect_format(rp.abs_path, ext)
        if fmt is None:
            raise errors.unsupported_media_type(detected=ext)

        try:
            size = rp.abs_path.stat().st_size
        except OSError:
            raise errors.artifact_path_not_found()

        render_mode = _RENDER_MODE[fmt]
        mime = _MIME[fmt]

        base = {
            "path": rp.rel_path,
            "name": name,
            "extension": fmt,
            "mime_type": mime,
            "size_bytes": size,
            "render_mode": render_mode,
            "content_type": mime,
            "encoding": None,
            "content": None,
            "stream_url": None,
            "converted_url": None,
            "download_allowed": False,
            "sanitized": False,
            "render_warnings": [],
        }

        if render_mode == "markdown":
            if size > max_inline_bytes:
                raise errors.file_too_large()
            text = rp.abs_path.read_text(encoding="utf-8", errors="replace")
            if sanitize:
                text, warnings = sanitize_markdown(text)
                base["sanitized"] = True
                base["render_warnings"] = warnings
            base["content"] = text
            base["content_type"] = "text/markdown; charset=utf-8"
            base["encoding"] = "utf-8"
            return {"file": base, "status": 200}

        if render_mode == "pdf_stream":
            from urllib.parse import quote

            base["stream_url"] = f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}"
            return {"file": base, "status": 200}

        if render_mode == "image":
            # UI-07: SVG 표시. <img src=stream_url> 로 안전 렌더(이미지로 로드 시 스크립트 비실행).
            # inline content 도 함께 제공하되 script/on*/javascript: 를 무력화(defense-in-depth).
            from urllib.parse import quote

            base["stream_url"] = f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}"
            if size <= max_inline_bytes:
                text = rp.abs_path.read_text(encoding="utf-8", errors="replace")
                if sanitize:
                    text, warnings = sanitize_markdown(text)
                    base["sanitized"] = True
                    base["render_warnings"] = warnings
                base["content"] = text
                base["encoding"] = "utf-8"
            base["content_type"] = "image/svg+xml"
            return {"file": base, "status": 200}

        if render_mode == "html":
            # UI-06: HTML 표시. FE 가 sandbox iframe 으로 렌더한다(스크립트 격리).
            # stream_url 로 원본을 제공(iframe src). raw HTML 본문은 inline 으로 내보내지 않는다
            # (iframe srcdoc 직접 주입 시 XSS 위험 — FE 는 src 로 로드하고 sandbox 적용).
            from urllib.parse import quote

            base["stream_url"] = f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}"
            base["content_type"] = "text/html; charset=utf-8"
            return {"file": base, "status": 200}

        # converted_preview (pptx/docx): 변환기는 DV/DevOps 단계 — 현재 미구현, pending 반환
        from urllib.parse import quote

        base["render_mode"] = "converted_preview"
        base["content_type"] = "text/html; charset=utf-8"
        base["encoding"] = "utf-8"
        base["converted_url"] = (
            f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}&variant=preview"
        )
        base["sanitized"] = True
        base["render_warnings"] = ["conversion_pending"]
        return {"file": base, "status": 202, "conversion": {"status": "pending", "timeout_seconds": 30}}

    def open_stream(self, raw_path: str, *, max_stream_bytes: int = 52_428_800) -> tuple[Path, str, int]:
        """PDF 등 원본 stream 용 검증된 경로 반환 (WG-ART-03)."""
        rp = self.resolve(raw_path)
        if not rp.abs_path.exists():
            raise errors.artifact_path_not_found()
        if rp.abs_path.is_dir():
            raise errors.not_file()
        ext = _ext(rp.abs_path.name)
        fmt = self.detect_format(rp.abs_path, ext)
        if fmt is None:
            raise errors.unsupported_media_type(detected=ext)
        size = rp.abs_path.stat().st_size
        return rp.abs_path, _MIME[fmt], size


def sanitize_markdown(text: str) -> tuple[str, list[str]]:
    """Markdown 원문 내 위험 raw HTML 1차 무력화 (DS-20 §13.4, TS-70).

    FE markdown 렌더러(markdown-it+sanitizer)가 최종 방어선이며, 본 함수는
    서버측 defense-in-depth 이다.
    """
    warnings: list[str] = []
    original = text

    # <script>...</script>, <iframe>, <style>, <object>, <embed> 제거
    def _strip(pattern: str, flags: int = re.IGNORECASE | re.DOTALL) -> None:
        nonlocal text
        text = re.sub(pattern, "", text, flags=flags)

    if re.search(r"<\s*(script|iframe|object|embed|style)\b", text, re.IGNORECASE):
        warnings.append("raw_html_stripped")
    _strip(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>")
    _strip(r"<\s*style\b[^>]*>.*?<\s*/\s*style\s*>")
    _strip(r"<\s*(iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>")
    _strip(r"<\s*(iframe|object|embed)\b[^>]*/?>")
    # 인라인 이벤트 핸들러 / javascript: URI
    if re.search(r"(?i)on\w+\s*=", text) or re.search(r"(?i)javascript:", text):
        warnings.append("inline_handlers_neutralized")
    text = re.sub(r"(?i)\son\w+\s*=\s*\"[^\"]*\"", "", text)
    text = re.sub(r"(?i)\son\w+\s*=\s*'[^']*'", "", text)
    text = re.sub(r"(?i)javascript:", "blocked:", text)

    if text == original and not warnings:
        return original, []
    return text, warnings
