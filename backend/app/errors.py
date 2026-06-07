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


def surface_not_found() -> WebguiError:
    return WebguiError("surface_not_found", 409, "No active cmux surface for target role.")


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


# --- 핸들러 등록 ------------------------------------------------------------

async def webgui_error_handler(_: Request, exc: WebguiError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_envelope())


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 내부 예외는 상세를 노출하지 않는다 (DS-40 §21).
    err = internal_error()
    return JSONResponse(status_code=err.http_status, content=err.to_envelope())
