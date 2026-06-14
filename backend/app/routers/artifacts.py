"""산출물 API (DV-20.3/.4): WG-ART-01/02/03.

설계: DS-40 §16~18, DS-20 §13, DS-60 §11.
모든 경로는 Artifact Service 를 통해서만 파일시스템에 접근한다 (allowlist+traversal 차단).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from .. import errors
from ..config import ROOT_TYPE_SUBDIR, get_settings
from ..deps import require_auth
from ..schemas.artifact import ArtifactWriteRequest
from ..schemas.common import ok
from ..services.artifact_service import ArtifactService

router = APIRouter(prefix="/api/webgui/artifacts", tags=["artifacts"])

# 코드탭/페르소나탭 추가(제우스 2026-06-14): 트리/파일 조회 root_type enum.
# documents = 산출물 문서 트리(현행), system = 코드(소스) 트리, persona = brain(역할 페르소나) 트리.
# 미지정/빈값 = documents(하위호환). 유효 값 집합은 config 매핑에서 파생 — 신규 탭 자동 동기화.
_VALID_ROOT_TYPES = tuple(ROOT_TYPE_SUBDIR.keys())


def _normalize_root_type(root_type: str | None) -> str:
    """root_type 정규화·검증. 미지정/빈값 → documents(하위호환). 미지의 값 → invalid_request(400)."""
    rt = (root_type or "").strip().lower()
    if rt == "":
        return "documents"
    if rt not in _VALID_ROOT_TYPES:
        raise errors.invalid_request(
            f"root_type must be one of: {', '.join(_VALID_ROOT_TYPES)}",
            details={"root_type": root_type},
        )
    return rt


def _svc(request: Request, project_id: str | None = None, root_type: str | None = None) -> ArtifactService:
    """project_id·root_type 별 산출물/코드 root 로 ArtifactService 해소 (QI-WG-024 + 코드탭).

    projects 엔드포인트와 동일한 project_root(project_id) 규약을 사용한다.
    project_id 미지정 시 settings.project_id 로 fallback. root_type 미지정/빈값 = documents.
    allowlist/traversal 보안은 해소된 per-project root(documents 또는 system) 기준으로 그대로
    적용된다(다른 프로젝트·상위 escape·symlink escape 차단). system 루트도 동일 안전성.
    """
    settings = get_settings()
    pid = project_id or settings.project_id
    rt = _normalize_root_type(root_type)
    root = settings.artifacts_root_for(pid, rt)
    display = settings.artifacts_display_root_for(pid, rt)
    return ArtifactService(root, display_root=display)


@router.get("/changes", dependencies=[Depends(require_auth)])
async def get_changes(
    request: Request,
    project_id: str = Query(...),
    after: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    """WG-ART-04 산출물 변경 이벤트 polling fallback (DS-40 §20).

    WebSocket 단절/reconnect 실패 중에도 산출물 폴더 변경을 화면에 반영하기 위한 fallback.
    반환 모델은 WebSocket `artifact_changed` 의 `data` 와 동일하다. project_id 격리 강제.
    """
    from ..services.artifact_watcher import CursorExpired, CursorParseError

    watcher = getattr(request.app.state, "artifact_watcher", None)
    if watcher is None or not getattr(watcher, "enabled", False):
        raise errors.artifact_watcher_unavailable()
    try:
        updates, next_cursor = watcher.buffer.changes_after(project_id, after, limit)
    except CursorParseError:
        raise errors.invalid_pagination("invalid after cursor format")
    except CursorExpired:
        raise errors.artifact_change_cursor_expired()
    return ok({"updates": updates, "next_cursor": next_cursor})


@router.get("/tree", dependencies=[Depends(require_auth)])
async def get_tree(
    request: Request,
    project_id: str | None = Query(default=None),
    root_type: str | None = Query(default=None),
    path: str | None = Query(default=None),
    depth: int = Query(default=1, ge=1),
    recursive: bool = Query(default=False),
    include_files: bool = Query(default=True),
    include_hidden: bool = Query(default=False),
    extensions: str | None = Query(default=None),
):
    settings = get_settings()
    ext_list = [e.strip().lower() for e in extensions.split(",") if e.strip()] if extensions else None
    data = _svc(request, project_id, root_type).list_tree(
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


@router.post("/write", dependencies=[Depends(require_auth)])
async def write_artifact(
    request: Request,
    body: ArtifactWriteRequest,
    project_id: str | None = Query(default=None),
    root_type: str | None = Query(default=None),
):
    """WG-ART-05 산출물 .md 저장.

    경로 체계는 GET /tree 와 동일(per-project root 기준 상대경로). 보안은
    ArtifactService.resolve() 가 GET 계열과 동일하게 적용한다(allowlist+traversal 차단).
    성공 200 {saved: true, path}, 경로 위반 403, .md 아님 400, 쓰기 실패 500.
    """
    data = _svc(request, project_id, root_type).write_file(body.path, body.content)
    return ok(data)


@router.get("/file", dependencies=[Depends(require_auth)])
async def get_file(
    request: Request,
    response: Response,
    path: str = Query(...),
    project_id: str | None = Query(default=None),
    root_type: str | None = Query(default=None),
    prefer: str = Query(default="inline"),
    sanitize: bool = Query(default=True),
):
    settings = get_settings()
    result = _svc(request, project_id, root_type).read_file(
        path,
        prefer=prefer,
        sanitize=sanitize,
        max_inline_bytes=settings.max_inline_bytes,
        root_type=_normalize_root_type(root_type),
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
    root_type: str | None = Query(default=None),
    variant: str = Query(default="original"),
):
    settings = get_settings()
    if variant == "preview":
        # pptx/docx 변환 preview 는 변환기 미구현 단계 -> render_pending (DS-40 §18.3)
        raise errors.WebguiError("render_pending", 202, "Conversion preview is not ready.")

    abs_path, mime, size = _svc(request, project_id, root_type).open_stream(path, max_stream_bytes=settings.max_stream_bytes)

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
