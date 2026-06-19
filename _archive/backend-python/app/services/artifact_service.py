"""산출물 서비스 (DV-20.3/.4).

설계: DS-20 §13 (접근 경계·렌더 경계), DS-40 §16~18, DS-60 §11.
보안 필수:
- allowlist 루트 = <project_root>/documents (QI-WG-024 정밀화, DS-20 §13.1).
  루트는 생성자 인자로 주입되며(_svc()=settings.artifacts_root_for(pid)), documents/ 전체가
  허용 범위다(reports/·products/ 등 모든 하위 포함). 아래 display_root 기본값은 fallback 표기일 뿐.
- traversal/절대경로/symlink escape/숨김·secret 차단 (DS-20 §13.2, DS-40 §16.4)
- 루트(=documents) 밖 상위 경로 접근 차단
- 응답/로그에 host 절대경로·파일본문(차단 시) 노출 금지

DB 미사용: 트리/메타/본문/렌더 입력은 매 요청 파일시스템에서 직접 읽는다 (DS-30 §10).
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .. import errors

# 지원 형식 (DS-40 §17.5)
_RENDER_MODE = {
    "md": "markdown",
    "markdown": "markdown",
    "pdf": "pdf_stream",
    "svg": "image",               # UI-07: SVG 이미지 표시 (FE ArtifactViewer 'image' 모드)
    # 17-3: 래스터 이미지 표시 (FE ArtifactViewer 'image' 모드, <img src=stream_url>)
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "gif": "image",
    "webp": "image",
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
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "html": "text/html",
    "htm": "text/html",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# 17-3: 래스터 이미지 확장자 집합 (svg 는 텍스트라 별도 취급)
_RASTER_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# 코드/텍스트 뷰어 (코드탭, 제우스 2026-06-14, 아테나 설계).
# 확장자 → CodeMirror language_hint. render_mode='code' 로 inline content + CodeMirror 표시/편집.
# ⚠️ md/markdown(=markdown), html/htm(=html)은 코드모드에서 제외(여기 넣지 않음 → 현행 유지).
_CODE_LANG = {
    "json": "json",
    "yaml": "yaml", "yml": "yaml",
    "toml": "toml",
    "ini": "ini", "cfg": "ini", "conf": "ini",
    "py": "python",
    "sh": "bash", "bash": "bash", "zsh": "bash",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "tsx": "typescript", "jsx": "javascript", "vue": "vue",
    "css": "css", "scss": "scss", "sass": "sass", "less": "less",
    "xml": "xml", "sql": "sql",
    "go": "go", "rs": "rust", "java": "java", "kt": "kotlin",
    "c": "c", "cpp": "cpp", "cc": "cpp", "h": "c", "hpp": "cpp",
    "rb": "ruby", "php": "php", "pl": "perl", "lua": "lua", "r": "r",
    "txt": "text", "log": "text", "csv": "text", "gitignore": "text",
    # 확장자 없는 특수 파일명(아래 _CODE_FILENAMES 로 매핑되는 pseudo-ext)
    "dockerfile": "dockerfile", "makefile": "makefile",
}
# 확장자 없는 코드 파일명 → pseudo-ext. (Dockerfile/Makefile). .gitignore 는 _ext 로 'gitignore' 잡힘.
_CODE_FILENAMES = {"dockerfile": "dockerfile", "makefile": "makefile"}
# code 확장자별 mime (대부분 text/plain, 구조화 포맷만 구체화). inline content 서빙이라 표시용.
_CODE_MIME_OVERRIDE = {"json": "application/json", "xml": "application/xml", "csv": "text/csv"}

# _RENDER_MODE/_MIME 를 코드 확장자로 보강(기존 md/html/pdf/image 항목은 setdefault 로 보존).
for _ext_key, _lang in _CODE_LANG.items():
    _RENDER_MODE.setdefault(_ext_key, "code")
    _MIME.setdefault(_ext_key, _CODE_MIME_OVERRIDE.get(_ext_key, "text/plain"))

# write 허용 확장자 = markdown + 코드/텍스트 allowlist (secret 은 resolve()가 먼저 차단).
_WRITABLE_EXTS = frozenset({"md", "markdown"}) | frozenset(_CODE_LANG)

# 업로드 허용 바이너리 확장자 (documents 루트에만, DS-132 §5.3). system/persona 는 거절.
_DOCUMENTS_BINARY_EXTS = frozenset({"pdf", "png", "jpg", "jpeg", "gif", "webp", "svg", "docx", "pptx"})
# 파일명 길이 상한 (UTF-8 문자 수 기준, DS-132 §4.2).
_FILENAME_MAX_LEN = 120

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


def _resolve_fmt_ext(name: str) -> str | None:
    """형식 판정용 effective 확장자. 일반 확장자 우선, 없으면 특수 파일명(Dockerfile/Makefile) 매핑.

    예: 'config.py'→'py', 'Dockerfile'→'dockerfile', 'Makefile'→'makefile', '.gitignore'→'gitignore'.
    """
    ext = _ext(name)
    if ext is not None:
        return ext
    low = name.lower()
    return _CODE_FILENAMES.get(low)


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
        # 형식 판정/렌더 가능 여부는 특수 파일명(Dockerfile/Makefile)까지 고려한 effective 확장자로.
        fmt_ext = None if is_dir else _resolve_fmt_ext(name)
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
            renderable = fmt_ext in _RENDER_MODE
        return {
            "path": rel_path,
            "name": name,
            "node_type": "directory" if is_dir else "file",
            "extension": ext,
            "mime_type": (None if is_dir else _MIME.get(fmt_ext or "")),
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
            # 17-3: 래스터 이미지 매직 시그니처 검증 (확장자 위조 차단)
            if ext in _RASTER_EXTS:
                try:
                    with open(abs_path, "rb") as f:
                        head = f.read(16)
                except OSError:
                    return None
                if ext == "png":
                    if not head.startswith(b"\x89PNG\r\n\x1a\n"):
                        return None
                elif ext in ("jpg", "jpeg"):
                    if not head.startswith(b"\xff\xd8\xff"):
                        return None
                elif ext == "gif":
                    if not (head.startswith(b"GIF87a") or head.startswith(b"GIF89a")):
                        return None
                elif ext == "webp":
                    # RIFF<4byte size>WEBP
                    if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
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
        root_type: str | None = None,
    ) -> dict:
        rp = self.resolve(raw_path)
        if not rp.abs_path.exists():
            raise errors.artifact_path_not_found()
        if rp.abs_path.is_dir():
            raise errors.not_file()

        name = rp.abs_path.name
        ext = _resolve_fmt_ext(name)
        fmt = self.detect_format(rp.abs_path, ext)
        if fmt is None:
            raise errors.unsupported_media_type(detected=ext)

        try:
            size = rp.abs_path.stat().st_size
        except OSError:
            raise errors.artifact_path_not_found()

        render_mode = _RENDER_MODE[fmt]
        mime = _MIME[fmt]

        # stream/converted URL 은 같은 root 로 재해소되어야 한다(documents/system/persona 불일치 방지).
        # documents 외(system·persona 등)일 때만 쿼리에 부착(미지정/documents 는 하위호환 위해 생략).
        _rt = (root_type or "").strip().lower()
        stream_root_qs = f"&root_type={_rt}" if _rt and _rt != "documents" else ""

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
            "language_hint": None,        # code 모드에서만 채움(CodeMirror 언어 ID)
        }

        if render_mode == "code":
            # 코드/텍스트 파일: inline content(텍스트 decode) + language_hint. CodeMirror 로 표시/편집.
            # 큰 파일 보호는 markdown 과 동일 정책(max_inline_bytes) 재사용.
            if size > max_inline_bytes:
                raise errors.file_too_large()
            text = rp.abs_path.read_text(encoding="utf-8", errors="replace")
            base["content"] = text
            base["content_type"] = "text/plain; charset=utf-8"
            base["encoding"] = "utf-8"
            base["language_hint"] = _CODE_LANG.get(fmt, "text")
            # 코드 본문은 sanitize 하지 않는다(HTML 렌더가 아니라 CodeMirror raw 표시).
            return {"file": base, "status": 200}

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

            base["stream_url"] = f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}{stream_root_qs}"
            return {"file": base, "status": 200}

        if render_mode == "image":
            from urllib.parse import quote

            base["stream_url"] = f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}{stream_root_qs}"
            if fmt == "svg":
                # UI-07: SVG 표시. <img src=stream_url> 로 안전 렌더(이미지로 로드 시 스크립트 비실행).
                # inline content 도 함께 제공하되 script/on*/javascript: 를 무력화(defense-in-depth).
                if size <= max_inline_bytes:
                    text = rp.abs_path.read_text(encoding="utf-8", errors="replace")
                    if sanitize:
                        text, warnings = sanitize_markdown(text)
                        base["sanitized"] = True
                        base["render_warnings"] = warnings
                    base["content"] = text
                    base["encoding"] = "utf-8"
                base["content_type"] = "image/svg+xml"
            else:
                # 17-3: png/jpg/jpeg/gif/webp 래스터 이미지 — 바이너리이므로 inline content 없이
                # stream_url 로만 서빙(<img src=stream_url>). content_type 은 정확한 image/* 로 내린다.
                base["content_type"] = mime
            return {"file": base, "status": 200}

        if render_mode == "html":
            # UI-06: HTML 표시. FE 가 sandbox iframe 으로 렌더한다(스크립트 격리).
            # stream_url 로 원본을 제공(iframe src). raw HTML 본문은 inline 으로 내보내지 않는다
            # (iframe srcdoc 직접 주입 시 XSS 위험 — FE 는 src 로 로드하고 sandbox 적용).
            from urllib.parse import quote

            base["stream_url"] = f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}{stream_root_qs}"
            base["content_type"] = "text/html; charset=utf-8"
            return {"file": base, "status": 200}

        # converted_preview (pptx/docx): 변환기는 DV/DevOps 단계 — 현재 미구현, pending 반환
        from urllib.parse import quote

        base["render_mode"] = "converted_preview"
        base["content_type"] = "text/html; charset=utf-8"
        base["encoding"] = "utf-8"
        base["converted_url"] = (
            f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}&variant=preview{stream_root_qs}"
        )
        base["sanitized"] = True
        base["render_warnings"] = ["conversion_pending"]
        return {"file": base, "status": 202, "conversion": {"status": "pending", "timeout_seconds": 30}}

    # --- 파일 쓰기 (WG-ART-05) --------------------------------------------

    def write_file(self, raw_path: str, content: str) -> dict:
        """산출물 .md 파일 저장.

        보안: resolve() 가 allowlist 루트 밖 접근(traversal/절대경로/symlink-escape/
        secret/hidden)을 차단한다(GET 계열과 동일 경계). 허용 확장자 = markdown + 코드/텍스트
        allowlist(_WRITABLE_EXTS). secret(.env·key·pem·token·credential·settings.local.json)은
        resolve() 가 먼저 차단하므로 allowlist 와 무관하게 쓰기 불가.

        - 경로 위반 → WebguiError(path_forbidden 403 등, resolve() 가 raise)
        - 허용 외 확장자 → invalid_artifact_type(400)
        - 대상이 디렉토리 → not_file(422)
        - 쓰기 실패 → artifact_write_failed(500)
        성공 시 {"saved": True, "path": <rel>} 반환.
        """
        rp = self.resolve(raw_path)
        ext = _resolve_fmt_ext(rp.abs_path.name)
        if ext not in _WRITABLE_EXTS:
            raise errors.invalid_artifact_type(detected=ext)
        # 기존 경로가 디렉토리면 덮어쓰기 금지
        if rp.abs_path.exists() and rp.abs_path.is_dir():
            raise errors.not_file()
        try:
            # 신규 산출물 작성 시 부모 디렉토리 보장 (루트 내부로 검증 완료된 경로).
            rp.abs_path.parent.mkdir(parents=True, exist_ok=True)
            rp.abs_path.write_text(content, encoding="utf-8")
        except OSError:
            raise errors.artifact_write_failed()
        return {"saved": True, "path": rp.rel_path}

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

    # --- 컨텍스트 메뉴 신규 (WG-ART-08/09 / DS-132) ------------------------

    def validate_filename(self, filename: str | None) -> str:
        """파일명 정책 검증 + NFC 정규화 반환 (DS-132 §4.2). 위반 시 적절한 WebguiError raise.

        경로 구분자는 '제거'가 아니라 거절(invalid_path). 숨김/secret 후보는 artifact_hidden.
        """
        raw = filename if filename is not None else ""
        if _CONTROL_CHARS.search(raw):
            raise errors.invalid_path()
        name = unicodedata.normalize("NFC", raw)
        if name.strip() == "":
            raise errors.invalid_request("filename is required.")
        # 경로 구분자 / Windows drive prefix → 거절(제거 아님)
        if "/" in name or "\\" in name:
            raise errors.invalid_path()
        if re.match(r"^[A-Za-z]:", name):
            raise errors.invalid_path()
        # 선행/후행 공백 거절
        if name != name.strip():
            raise errors.invalid_path()
        if len(name) > _FILENAME_MAX_LEN:
            raise errors.invalid_path()
        if name in (".", ".."):
            raise errors.invalid_path()
        if name.startswith("."):
            raise errors.artifact_hidden()
        if _SECRET_NAME.search(name):
            raise errors.artifact_hidden()
        return name

    def _resolve_parent_dir(self, parent_path: str | None) -> ResolvedPath:
        """폴더 한정 기능용: parent_path resolve 후 반드시 is_dir 강제 (DS-132 §7)."""
        rp = self.resolve(parent_path)
        if not rp.abs_path.exists():
            raise errors.artifact_path_not_found()
        if not rp.abs_path.is_dir():
            raise errors.not_directory()
        return rp

    @staticmethod
    def _verify_signature(data: bytes, ext: str) -> bool:
        """업로드 바이너리 magic bytes/signature 검증 (DS-132 §5.3, detect_format 정합)."""
        head = data[:16]
        if ext == "pdf":
            return data[:5] == b"%PDF-"
        if ext in ("docx", "pptx"):
            return data[:2] == b"PK"
        if ext == "png":
            return head.startswith(b"\x89PNG\r\n\x1a\n")
        if ext in ("jpg", "jpeg"):
            return head.startswith(b"\xff\xd8\xff")
        if ext == "gif":
            return head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
        if ext == "webp":
            return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        if ext == "svg":
            h = data[:1024].lstrip(b"\xef\xbb\xbf").lstrip()
            return h.startswith(b"<?xml") or h.startswith(b"<svg") or b"<svg" in h
        return True  # 텍스트/코드는 signature 검사 없음(decode 검증으로 대체)

    def _rename_candidates(self, name: str):
        """충돌 회피용 후보명: name, 'name (1).ext', ... 100회 (DS-132 §4.4)."""
        yield name
        if "." in name and not name.startswith("."):
            stem, ext = name.rsplit(".", 1)
            for i in range(1, 101):
                yield f"{stem} ({i}).{ext}"
        else:
            for i in range(1, 101):
                yield f"{name} ({i})"

    def _first_free_name(self, parent_abs: Path, name: str) -> str:
        for cand in self._rename_candidates(name):
            if not (parent_abs / cand).exists():
                return cand
        raise errors.artifact_already_exists({"filename": name})

    def _tree_refresh(self, root_type: str | None, parent_rel: str, changed_rel: str) -> dict:
        rt = (root_type or "").strip().lower() or "documents"
        return {
            "root_type": rt,
            "parent_path": parent_rel,
            "changed_path": changed_rel,
            "change_type": "created",
        }

    def _describe_written_file(self, rel_path: str, root_type: str | None, max_inline_bytes: int) -> dict:
        """생성/업로드 직후 WG-ART-02 ArtifactFile 전체 필드 반환 (축약 금지, DS-132 §3).

        read_file(WG-ART-02)를 그대로 재사용해 Python/Rust 동일 스키마를 보장한다. 1MiB 초과
        텍스트(file_too_large)는 inline content 없이 메타+stream_url 만 반환한다.
        """
        try:
            return self.read_file(
                rel_path,
                prefer="inline",
                sanitize=True,
                max_inline_bytes=max_inline_bytes,
                root_type=root_type,
            )["file"]
        except errors.WebguiError as exc:
            if exc.code != "file_too_large":
                raise
        rp = self.resolve(rel_path)
        name = rp.abs_path.name
        fmt = self.detect_format(rp.abs_path, _resolve_fmt_ext(name))
        mime = _MIME[fmt]
        _rt = (root_type or "").strip().lower()
        qs = f"&root_type={_rt}" if _rt and _rt != "documents" else ""
        return {
            "path": rp.rel_path,
            "name": name,
            "extension": fmt,
            "mime_type": mime,
            "size_bytes": rp.abs_path.stat().st_size,
            "render_mode": _RENDER_MODE[fmt],
            "content_type": mime,
            "encoding": None,
            "content": None,
            "stream_url": f"/api/webgui/artifacts/file/stream?path={quote(rp.rel_path)}{qs}",
            "converted_url": None,
            "download_allowed": False,
            "sanitized": False,
            "render_warnings": [],
            "language_hint": _CODE_LANG.get(fmt),
        }

    def create_file(
        self,
        parent_path: str | None,
        filename: str,
        *,
        template: str = "empty",
        if_exists: str = "error",
        root_type: str | None = None,
        max_inline_bytes: int = 1_048_576,
    ) -> dict:
        """WG-ART-08 새파일 생성 (DS-132 §4). 폴더 한정: parent_path 는 디렉토리여야 한다.

        보안은 resolve()(traversal/절대/symlink/secret 차단)와 validate_filename(숨김/secret/
        구분자/길이)로 GET 계열과 동일하게 적용된다. 원자 생성은 O_CREAT|O_EXCL.
        """
        parent = self._resolve_parent_dir(parent_path)
        name = self.validate_filename(filename)
        ext = _resolve_fmt_ext(name)
        # 확장자 allowlist = writable(텍스트/코드). html/htm 은 새파일 생성 기본 제외(§4.3).
        if ext not in _WRITABLE_EXTS:
            raise errors.unsupported_media_type(detected=ext)
        if template not in ("empty", "markdown_basic", "json_object"):
            raise errors.invalid_request("invalid template", {"template": template})
        if if_exists not in ("error", "rename"):
            raise errors.invalid_request("invalid if_exists", {"if_exists": if_exists})
        if template == "json_object" and ext != "json":
            raise errors.invalid_artifact_template("json_object requires a .json extension")
        if template == "markdown_basic" and ext not in ("md", "markdown"):
            raise errors.invalid_artifact_template("markdown_basic requires a .md extension")

        basename = name.rsplit(".", 1)[0] if "." in name else name
        if template == "markdown_basic":
            content = f"# {basename}\n"
        elif template == "json_object":
            content = "{}\n"
        else:
            content = ""

        final_name = self._atomic_create(parent.abs_path, name, content.encode("utf-8"), if_exists)
        rel = f"{parent.rel_path}/{final_name}" if parent.rel_path else final_name
        file_dict = self._describe_written_file(rel, root_type, max_inline_bytes)
        return {"file": file_dict, "tree_refresh": self._tree_refresh(root_type, parent.rel_path, rel)}

    def _atomic_create(self, parent_abs: Path, name: str, data: bytes, if_exists: str) -> str:
        """O_CREAT|O_EXCL 원자 생성. error 모드 충돌 → 409. rename 모드 → 빈 이름 탐색."""
        candidates = [name] if if_exists == "error" else list(self._rename_candidates(name))
        for cand in candidates:
            target = parent_abs / cand
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if if_exists == "error":
                    raise errors.artifact_already_exists({"filename": cand})
                continue
            except OSError:
                raise errors.artifact_write_failed()
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            except OSError:
                try:
                    os.unlink(target)
                except OSError:
                    pass
                raise errors.artifact_write_failed()
            return cand
        raise errors.artifact_already_exists({"filename": name})

    def upload_file(
        self,
        parent_path: str | None,
        filename: str,
        data: bytes,
        *,
        if_exists: str = "rename",
        root_type: str | None = None,
        client_upload_id: str | None = None,
        max_upload_bytes: int = 26_214_400,
        max_inline_bytes: int = 1_048_576,
    ) -> dict:
        """WG-ART-09 파일 업로드 (DS-132 §5). 폴더 한정 + root_type 별 확장자 정책 +
        크기 상한 + 확장자/signature/decode 검증. temp 기록 → atomic rename, 실패 시 cleanup.
        """
        rt = (root_type or "").strip().lower() or "documents"
        if if_exists not in ("error", "rename"):
            raise errors.invalid_request("invalid if_exists", {"if_exists": if_exists})
        parent = self._resolve_parent_dir(parent_path)
        name = self.validate_filename(filename)
        if len(data) > max_upload_bytes:
            raise errors.file_too_large()

        ext = _resolve_fmt_ext(name)
        if ext is None:
            # 확장자 없음: documents 거절. system/persona 의 Dockerfile/Makefile 은 _resolve_fmt_ext 가 잡음.
            raise errors.unsupported_media_type(detected=None)
        is_binary = ext in _DOCUMENTS_BINARY_EXTS
        if rt == "documents":
            allowed = (ext in _WRITABLE_EXTS) or is_binary
        else:  # system/persona: 텍스트/코드만, 바이너리 거절(§5.2)
            allowed = (ext in _WRITABLE_EXTS) and not is_binary
        if not allowed:
            raise errors.unsupported_media_type(detected=ext)

        if is_binary:
            if not self._verify_signature(data, ext):
                raise errors.unsupported_media_type(detected=ext)
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise errors.invalid_text_encoding()

        final_name = self._atomic_upload(parent.abs_path, name, data, if_exists)
        rel = f"{parent.rel_path}/{final_name}" if parent.rel_path else final_name
        sha = hashlib.sha256(data).hexdigest()
        file_dict = self._describe_written_file(rel, rt, max_inline_bytes)
        upload = {
            "client_upload_id": client_upload_id,
            "filename": final_name,
            "mime_type": file_dict["mime_type"],
            "size_bytes": len(data),
            "sha256": sha,
        }
        return {"upload": upload, "file": file_dict, "tree_refresh": self._tree_refresh(rt, parent.rel_path, rel)}

    def _atomic_upload(self, parent_abs: Path, name: str, data: bytes, if_exists: str) -> str:
        """temp 파일 기록 → target atomic rename. error 모드는 O_EXCL 로 target 선점."""
        if if_exists == "error":
            final_name = name
            target = parent_abs / final_name
            try:
                fd_excl = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.close(fd_excl)
            except FileExistsError:
                raise errors.artifact_already_exists({"filename": final_name})
            except OSError:
                raise errors.artifact_storage_unavailable()
        else:
            final_name = self._first_free_name(parent_abs, name)
            target = parent_abs / final_name

        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent_abs), prefix=".upload-", suffix=".tmp")
        except OSError:
            if if_exists == "error":
                try:
                    os.unlink(target)
                except OSError:
                    pass
            raise errors.artifact_storage_unavailable()
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if if_exists == "error":
                try:
                    os.unlink(target)
                except OSError:
                    pass
            raise errors.artifact_write_failed()
        return final_name


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
