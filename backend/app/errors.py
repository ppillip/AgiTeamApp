"""오류 envelope 및 표준 오류코드 (DS-40 §3.4, §20).

모든 오류는 공통 envelope 로 반환한다:
  {"ok": false, "error": {"code": "...", "message": "...", "details": {...}}}

details 에는 secret/token/로컬 절대경로/stack trace 를 포함하지 않는다 (DS-40 §21).
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class WebguiError(Exception):
    """DS-40 표준 오류. http_status + error.code 를 함께 운반한다."""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = details or {}
        super().__init__(f"{code}: {message}")

    def to_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


# --- DS-40 §20 오류코드 팩토리 (자주 쓰는 것) -------------------------------

def unauthorized(msg: str = "Authentication required.") -> WebguiError:
    return WebguiError("unauthorized", 401, msg)


def forbidden(msg: str = "Access denied.") -> WebguiError:
    return WebguiError("forbidden", 403, msg)


def invalid_request(msg: str = "Invalid request.", details: dict | None = None) -> WebguiError:
    return WebguiError("invalid_request", 400, msg, details)


def empty_message() -> WebguiError:
    return WebguiError("empty_message", 422, "Message text is empty.")


def room_not_found(msg: str = "Room not found.") -> WebguiError:
    return WebguiError("room_not_found", 404, msg)


def message_not_found() -> WebguiError:
    return WebguiError("message_not_found", 404, "Message not found.")


def room_role_mismatch() -> WebguiError:
    return WebguiError("room_role_mismatch", 409, "room_id and role_id do not match.")


def not_ready() -> WebguiError:
    return WebguiError("not_ready", 409, "Target role is not ready.")


def surface_not_found(details: dict | None = None) -> WebguiError:
    # details 예: {"reason": "not_terminal"} — non-terminal surface 로 ping 실패한 경우 진단용
    # (QI-WG-021/023). secret/절대경로는 담지 않는다(DS-40 §21).
    return WebguiError("surface_not_found", 409, "No active cmux surface for target role.", details)


def send_failed(details: dict | None = None) -> WebguiError:
    return WebguiError("send_failed", 502, "cmux send failed.", details)


def db_unavailable() -> WebguiError:
    # DB 연결 불가. 로컬 dev 에서 PostgreSQL 미가동 시 발생.
    return WebguiError("internal_error", 503, "Storage is currently unavailable.")


def internal_error() -> WebguiError:
    return WebguiError("internal_error", 500, "Internal server error.")


# 산출물 오류 (DS-40 §16.4 / §17.7)
def invalid_path() -> WebguiError:
    return WebguiError("invalid_path", 400, "Invalid artifact path.")


def path_forbidden() -> WebguiError:
    return WebguiError("path_forbidden", 403, "Path is outside the allowed root.")


def artifact_path_not_found() -> WebguiError:
    return WebguiError("artifact_path_not_found", 404, "Artifact path not found.")


def not_directory() -> WebguiError:
    return WebguiError("not_directory", 422, "Target is not a directory.")


def not_file() -> WebguiError:
    return WebguiError("not_file", 422, "Target is not a file.")


def artifact_hidden() -> WebguiError:
    return WebguiError("artifact_hidden", 403, "Hidden or restricted file.")


def symlink_forbidden() -> WebguiError:
    return WebguiError("symlink_forbidden", 403, "Symbolic links are not allowed.")


def unsupported_media_type(detected: str | None = None) -> WebguiError:
    d = {"detected_format": detected} if detected else None
    return WebguiError("unsupported_media_type", 415, "Unsupported file format.", d)


def file_too_large() -> WebguiError:
    return WebguiError("file_too_large", 413, "File exceeds the allowed size.")


def render_failed(reason: str | None = None) -> WebguiError:
    d = {"reason": reason} if reason else None
    return WebguiError("render_failed", 422, "Failed to render file.", d)


def render_timeout() -> WebguiError:
    return WebguiError("render_timeout", 504, "Render/convert timed out.")


def invalid_tree_query(msg: str = "Invalid tree query.") -> WebguiError:
    return WebguiError("invalid_tree_query", 422, msg)


# 산출물 저장 (WG-ART-05 파일 쓰기). md 만 허용(400), 쓰기 실패(500).
def invalid_artifact_type(detected: str | None = None) -> WebguiError:
    d = {"detected_format": detected} if detected else None
    return WebguiError("invalid_artifact_type", 400, "Only .md artifacts can be saved.", d)


def artifact_write_failed() -> WebguiError:
    return WebguiError("artifact_write_failed", 500, "Failed to write artifact.")


# 산출물 변경 polling (WG-ART-04 / DS-40 §20.4)
def invalid_pagination(msg: str = "Invalid pagination cursor.") -> WebguiError:
    return WebguiError("invalid_pagination", 422, msg)


def artifact_change_cursor_expired() -> WebguiError:
    return WebguiError(
        "artifact_change_cursor_expired", 409, "Change cursor is outside the retained buffer."
    )


def artifact_watcher_unavailable() -> WebguiError:
    return WebguiError(
        "artifact_watcher_unavailable", 503, "Artifact watcher is not active."
    )


# 이미지 첨부 (WG-MSG-06 / DS-40 §7.6.5, DS-120)
def project_not_found() -> WebguiError:
    return WebguiError("project_not_found", 404, "Project not found.")


def unsupported_image_type() -> WebguiError:
    return WebguiError("unsupported_image_type", 415, "Unsupported image format.")


def invalid_image() -> WebguiError:
    return WebguiError("invalid_image", 422, "File is not a valid image.")


def attachment_too_large() -> WebguiError:
    return WebguiError("attachment_too_large", 413, "Image exceeds the allowed size.")


def attachment_storage_unavailable() -> WebguiError:
    return WebguiError("attachment_storage_unavailable", 503, "Attachment storage is unavailable.")


def attachment_not_found() -> WebguiError:
    return WebguiError("attachment_not_found", 404, "Attachment not found.")


def attachment_expired() -> WebguiError:
    return WebguiError("attachment_expired", 410, "Attachment has expired.")


def too_many_attachments() -> WebguiError:
    return WebguiError("too_many_attachments", 413, "Too many attachments for one message.")


# AttachmentService 오류 code → WebguiError 매핑 (DS-40 §7.6.5)
_ATTACHMENT_ERROR_MAP = {
    "unsupported_image_type": unsupported_image_type,
    "invalid_image": invalid_image,
    "attachment_too_large": attachment_too_large,
    "attachment_storage_unavailable": attachment_storage_unavailable,
    "attachment_not_found": attachment_not_found,
    "attachment_expired": attachment_expired,
    "too_many_attachments": too_many_attachments,
}


def attachment_error(code: str) -> WebguiError:
    factory = _ATTACHMENT_ERROR_MAP.get(code)
    return factory() if factory else internal_error()


# --- 핸들러 등록 ------------------------------------------------------------

async def webgui_error_handler(_: Request, exc: WebguiError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_envelope())


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 내부 예외는 상세를 노출하지 않는다 (DS-40 §21).
    err = internal_error()
    return JSONResponse(status_code=err.http_status, content=err.to_envelope())
