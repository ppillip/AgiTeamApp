"""FastAPI 앱 팩토리 (DV-20).

확정 스택: Python + FastAPI (DS-20 §15.3).
앱은 PostgreSQL 미가동 환경에서도 기동되며, DB 의존 엔드포인트만 503 으로 처리한다.
산출물(FS) 엔드포인트는 DB 없이 동작한다.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .db.base import dispose_engine, get_sessionmaker
from .errors import (
    WebguiError,
    invalid_request,
    unhandled_error_handler,
    webgui_error_handler,
)
from .routers import artifacts, collector, messages, projects, rooms, runtime
from .services.artifact_service import ArtifactService
from .services.background import BackgroundManager
from .services.cmux_discovery import registry as discovery_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.artifact_service = ArtifactService(settings.artifacts_root_resolved)
    app.state.registry = discovery_registry
    app.state.background = BackgroundManager()
    if settings.enable_background:
        app.state.background.start(settings, discovery_registry, get_sessionmaker())
    yield
    await app.state.background.stop()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AgiTeamApp WebGUI Backend",
        version=__version__,
        description="DV-20 메시지 채널·PM 브릿지·팀원별 채팅·산출물 브라우저 (DS-20/30/40/60)",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:1420", "http://127.0.0.1:1420"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 오류 핸들러
    app.add_exception_handler(WebguiError, webgui_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        err = invalid_request("Request validation failed.", {"errors": _safe_errors(exc)})
        return JSONResponse(status_code=err.http_status, content=err.to_envelope())

    # 라우터
    app.include_router(projects.router)
    app.include_router(runtime.router)
    app.include_router(messages.router)
    app.include_router(rooms.router)
    app.include_router(collector.router)
    app.include_router(artifacts.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"ok": True, "data": {"status": "ok", "version": __version__, "project_id": settings.project_id}}

    @app.get("/", tags=["meta"])
    async def root():
        return {"ok": True, "data": {"service": "agiteamapp-webgui-backend", "docs": "/docs"}}

    return app


def _safe_errors(exc: RequestValidationError) -> list[dict]:
    # secret/본문 원문 노출 방지: 위치/타입만 남긴다.
    out = []
    for e in exc.errors():
        out.append({"loc": [str(x) for x in e.get("loc", [])], "type": e.get("type")})
    return out


app = create_app()
