"""이미지 첨부 업로드/미리보기 API (DV-90): WG-MSG-06.

설계: DS-40 §7.6, DS-60 §5.4.
- POST /api/webgui/message-attachments/images : multipart 업로드 → attachment_id 발급
- GET  /api/webgui/message-attachments/{attachment_id}/preview : 썸네일/미리보기 binary

브라우저 paste blob 또는 파일 선택 업로드를 Backend 임시 저장소에 저장한다. PM 에 직접
송신하지 않고 WG-MSG-02 가 참조할 `attachment_id` 만 발급한다. 공개 응답에 절대경로 미노출.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse

from .. import errors
from ..config import get_settings
from ..deps import require_auth
from ..schemas.common import ok
from ..services.attachment_service import (
    AttachmentError,
    AttachmentService,
    resolve_attachment_globally,
)

router = APIRouter(prefix="/api/webgui/message-attachments", tags=["attachments"])


def _service(project_id: str) -> AttachmentService:
    settings = get_settings()
    if not settings.project_exists(project_id):
        raise errors.project_not_found()
    return AttachmentService(
        settings.project_root(project_id),
        max_bytes=settings.attachment_max_bytes,
        ttl_seconds=settings.attachment_ttl_seconds,
    )


@router.post("/images", dependencies=[Depends(require_auth)])
async def upload_image(
    response: Response,
    project_id: str = Form(...),
    file: UploadFile = File(...),
    client_attachment_id: str | None = Form(default=None),
):
    """WG-MSG-06 이미지 첨부 업로드 (multipart/form-data)."""
    svc = _service(project_id)
    settings = get_settings()
    # 용량 상한: 스트리밍 누적 검증(전체 메모리 적재 전 차단)
    data = await file.read(settings.attachment_max_bytes + 1)
    if len(data) > settings.attachment_max_bytes:
        raise errors.attachment_too_large()
    try:
        stored = svc.save(
            project_id=project_id,
            data=data,
            declared_filename=file.filename,
            client_attachment_id=client_attachment_id,
        )
    except AttachmentError as exc:
        raise errors.attachment_error(exc.code)
    response.status_code = 201
    return ok({"attachment": stored.public_dict()})


@router.get("/{attachment_id}/preview", dependencies=[Depends(require_auth)])
async def preview_image(request: Request, attachment_id: str):
    """WG-MSG-06.7 self-contained 썸네일/미리보기 (DS-40 §7.6.7).

    `project_id` query 에 의존하지 않는다. `attachment_id` 만으로 등록된 project root 들의
    sidecar JSON 을 찾아 소유 project 를 해소하고, 기존 경로보안(allowlist/traversal/
    root containment)·TTL·project_exists 를 검증한 뒤 binary image 만 반환한다.

    오류 매핑(§7.6.7): ID형식·sidecar없음·project불일치·root escape → 404 attachment_not_found,
    TTL/파일누락 → 410 attachment_expired.
    """
    settings = get_settings()
    registry = getattr(request.app.state, "registry", None)
    try:
        _pid, stored = resolve_attachment_globally(settings, registry, attachment_id)
    except AttachmentError as exc:
        raise errors.attachment_error(exc.code)
    # FileResponse 는 파일 내용만 반환하고 host 절대경로를 응답 body/헤더에 노출하지 않는다.
    return FileResponse(
        stored.abs_path,
        media_type=stored.mime_type,
        headers={"Content-Security-Policy": "default-src 'none'", "X-Content-Type-Options": "nosniff"},
    )
