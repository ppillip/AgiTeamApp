"""의존성: 인증, DB 세션 (DS-40 §3.2)."""
from __future__ import annotations

from typing import AsyncIterator

from fastapi import Header, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from . import errors
from .config import Settings, get_settings
from .db.base import get_sessionmaker


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def require_auth(
    authorization: str | None = Header(default=None),
    webgui_session: str | None = Header(default=None, alias="X-Webgui-Session"),
) -> None:
    """일반 WebGUI API 인증.

    api_token 미설정(로컬 dev) 이면 인증을 생략한다 (구현 방식은 DV/운영에서 확정, DS-40 §3.2).
    """
    settings = get_settings()
    if not settings.auth_required:
        return
    token = _extract_bearer(authorization) or webgui_session
    if token != settings.api_token:
        raise errors.unauthorized()


async def require_collector_auth(
    authorization: str | None = Header(default=None),
) -> None:
    """WG-CHAT-05/06 내부 collector 전용 인증 (일반 토큰과 분리, DS-40 §21)."""
    settings = get_settings()
    if not settings.collector_auth_required:
        return
    token = _extract_bearer(authorization)
    if token != settings.collector_token:
        raise errors.unauthorized()


async def get_db() -> AsyncIterator[AsyncSession]:
    """DB 세션 의존성. 연결 불가(PostgreSQL 미가동) 시 503 으로 변환."""
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            try:
                yield session
            except errors.WebguiError:
                await session.rollback()
                raise
            except SQLAlchemyError:
                await session.rollback()
                raise errors.db_unavailable()
    except errors.WebguiError:
        raise
    except (SQLAlchemyError, OSError, ConnectionError):
        raise errors.db_unavailable()


def settings_dep() -> Settings:
    return get_settings()


def artifacts_root(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.artifact_service
