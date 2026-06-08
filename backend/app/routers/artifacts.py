"""산출물 API (DV-20.3/.4): WG-ART-01/02/03.

설계: DS-40 §16~18, DS-20 §13, DS-60 §11.
모든 경로는 Artifact Service 를 통해서만 파일시스템에 접근한다 (allowlist+traversal 차단).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from .. import errors
from ..config import get_settings
from ..deps import require_auth
from ..schemas.common import ok
from ..services.artifact_service import ArtifactService

router = APIRouter(prefix="/api/webgui/artifacts", tags=["artifacts"])


def _svc(request: Request, project_id: str | None = None) -> ArtifactService:
    """project_id 별 산출물 root 로 ArtifactService 해소 (QI-WG-024).

    projects 엔드포인트와 동일한 project_root(project_id) 규약을 사용한다.
    project_id 미지정 시 settings.project_id 로 fallback. allowlist/traversal 보안은
    해소된 per-project root 기준으로 그대로 적용된다(다른 프로젝트·상위 escape 차단).
    """
    settings = get_settings()
    pid = project_id or settings.project_id
    root = settings.artifacts_root_for(pid)
    display = settings.artifacts_display_root_for(pid)
    return ArtifactService(root, display_root=display)


@router.get("/tree", dependencies=[Depends(require_auth)])
async def get_tree(
    request: Request,
    project_id: str | None = Query(default=None),
    path: str | None = Query(default=None),
    depth: int = Query(default=1, ge=1),
    recursive: bool = Query(default=False),
    include_files: bool = Query(default=True),
    include_hidden: bool = Query(default=False),
    extensions: str | None = Query(default=None),
):
    settings = get_settings()
    ext_list = [e.strip().lower() for e in extensions.split(",") if e.strip()] if extensions else None
    data = _svc(request, project_id).list_tree(
        path,
        depth=depth,
        recursive=recursive,
        include_files=include_files,
        include_hidden=include_hidden,
        extensions=ext_list,
        max_nodes=settings.max_tree_nodes,
        max_depth=settings.max_tree_depth,
    )
    return ok(data)


@router.get("/file", dependencies=[Depends(require_auth)])
async def get_file(
    request: Request,
    response: Response,
    path: str = Query(...),
    project_id: str | None = Query(default=None),
    prefer: str = Query(default="inline"),
    sanitize: bool = Query(default=True),
):
    settings = get_settings()
    result = _svc(request, project_id).read_file(
        path,
        prefer=prefer,
        sanitize=sanitize,
        max_inline_bytes=settings.max_inline_bytes,
    )
    response.status_code = result.get("status", 200)
    data = {"file": result["file"]}
    if "conversion" in result:
        data["conversion"] = result["conversion"]
    return ok(data)


@router.get("/file/stream", dependencies=[Depends(require_auth)])
async def stream_file(
    request: Request,
    path: str = Query(...),
    project_id: str | None = Query(default=None),
    variant: str = Query(default="original"),
):
    settings = get_settings()
    if variant == "preview":
        # pptx/docx 변환 preview 는 변환기 미구현 단계 -> render_pending (DS-40 §18.3)
        raise errors.WebguiError("render_pending", 202, "Conversion preview is not ready.")

    abs_path, mime, size = _svc(request, project_id).open_stream(path, max_stream_bytes=settings.max_stream_bytes)

    range_header = request.headers.get("range")
    start, end = 0, size - 1
    status_code = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        "X-Content-Type-Options": "nosniff",
    }
    # UI-06/07: html/svg 는 직접 탐색 시에도 스크립트가 앱 오리진에서 실행되지 않도록
    # CSP sandbox 강제(FE 는 sandbox iframe / <img> 로 렌더하므로 정상 표시에는 영향 없음).
    if mime in ("text/html", "image/svg+xml"):
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    if range_header and range_header.startswith("bytes="):
        try:
            rng = range_header.split("=", 1)[1].split(",")[0]
            s, _, e = rng.partition("-")
            start = int(s) if s else 0
            end = int(e) if e else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        except ValueError:
            start, end, status_code = 0, size - 1, 200

    length = end - start + 1
    headers["Content-Length"] = str(length)

    def _iter():
        with open(abs_path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(_iter(), status_code=status_code, headers=headers, media_type=mime)
