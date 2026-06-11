"""PM Bridge 이미지 경로 주입/분기 단위테스트 (DV-90 / DS-60 §5.4)."""
from __future__ import annotations

import struct
import zlib
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.config import Settings
from app.db.base import Base
from app.services import events as events_mod
from app.services.attachment_service import AttachmentService
from app.services.cmux_discovery import DiscoveryRegistry, SurfaceInfo
from app.services.pm_bridge import PMBridge, compose_submit_text


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001, ANN202
    return "JSON"


def _png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", 12, 8) + b"\x08\x06\x00\x00\x00"
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


# --- 순수 합성 함수 (DS-60 §5.4.3/§5.4.4) ---


def test_compose_claude_format():
    out = compose_submit_text("이 화면 봐줘", ["/abs/a.png", "/abs/b.jpg"], "claude_code")
    lines = out.split("\n")
    assert lines[0] == "이 화면 봐줘"
    assert lines[1] == ""
    assert lines[2] == "[Image: source: /abs/a.png]"
    assert lines[3] == "[Image: source: /abs/b.jpg]"  # 순서 보존
    assert "@" not in out  # @경로 미사용


def test_compose_unknown_defaults_to_claude():
    out = compose_submit_text("hi", ["/abs/a.png"], None)
    assert "[Image: source: /abs/a.png]" in out
    out2 = compose_submit_text("hi", ["/abs/a.png"], "")
    assert "[Image: source: /abs/a.png]" in out2


def test_compose_codex_format():
    out = compose_submit_text("스샷", ["/abs/a.png", "/abs/b.png"], "codex")
    assert "첨부 이미지 파일 경로:" in out
    assert "/abs/a.png" in out and "/abs/b.png" in out
    assert "[Image: source:" not in out  # codex 는 Claude 형식 미사용


def test_compose_empty_text_uses_default_line():
    out = compose_submit_text("", ["/abs/a.png"], "claude_code")
    assert out.split("\n")[0] == "첨부 이미지를 확인하세요."


def test_compose_no_attachments_passthrough():
    assert compose_submit_text("그냥 텍스트", [], "claude_code") == "그냥 텍스트"


# --- send() 통합 (경로 주입·DB 저장·공개응답 분리) ---


@pytest_asyncio.fixture
async def sessionmaker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pg.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


class _FakeAdapter:
    def __init__(self) -> None:
        self.submitted_text: str | None = None

    async def tree(self):
        return None

    async def runtime_metadata(self, tree):
        return {}

    async def read_screen(self, surface_id, *, lines=1, workspace_id="", tty=None):
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def submit(self, surface_id, text, workspace_id, tty):
        self.submitted_text = text  # 제출 텍스트 캡처(절대경로 포함 검증)
        return {"submitted": True, "ended_at": "2026-06-08T00:00:00+00:00"}


def _registry(project_id: str, agent_type: str | None) -> DiscoveryRegistry:
    reg = DiscoveryRegistry()
    reg._map[(project_id, "PM")] = SurfaceInfo(  # noqa: SLF001
        project_id=project_id,
        role_id="PM",
        surface_id="surface:01",
        display_name="PM",
        connection_state="connected",
        last_seen_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        workspace_id="ws-1",
        tty="ttys001",
        agent_type=agent_type,
    )
    return reg


@pytest.mark.asyncio
async def test_send_injects_abs_path_into_submit_not_db(sessionmaker, tmp_path, monkeypatch):
    project_id = "AttProj"
    proj_root = tmp_path / project_id
    (proj_root / ".agiteam").mkdir(parents=True)
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", f'{{"{project_id}": "{proj_root}"}}')
    settings = Settings()
    att_svc = AttachmentService(proj_root, ttl_seconds=3600)
    stored = att_svc.save(project_id=project_id, data=_png(), declared_filename=None)

    adapter = _FakeAdapter()
    bridge = PMBridge(settings, adapter=adapter, registry=_registry(project_id, "claude_code"))
    monkeypatch.setattr(events_mod.hub, "publish", _noop_publish)

    async with sessionmaker() as db:
        result = await bridge.send(
            db,
            project_id=project_id,
            text="이 화면 분석해줘",
            attachments=[{"attachment_id": stored.attachment_id}],
            attachment_service=att_svc,
        )

    # 제출 텍스트(cmux)에는 절대경로 포함
    assert stored.abs_path in adapter.submitted_text
    assert "[Image: source:" in adapter.submitted_text
    # DB 공개 text 에는 절대경로 미포함(사용자 원문만) — transcript dedupe 보호
    assert result["message"]["text"] == "이 화면 분석해줘"
    assert stored.abs_path not in str(result["message"]["text"])
    # 공개 attachments 메타 노출(절대경로 없음)
    atts = result["message"]["attachments"]
    assert len(atts) == 1
    assert atts[0]["attachment_id"] == stored.attachment_id
    assert stored.abs_path not in str(atts)


@pytest.mark.asyncio
async def test_send_codex_branch(sessionmaker, tmp_path, monkeypatch):
    project_id = "CodexProj"
    proj_root = tmp_path / project_id
    (proj_root / ".agiteam").mkdir(parents=True)
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", f'{{"{project_id}": "{proj_root}"}}')
    settings = Settings()
    att_svc = AttachmentService(proj_root, ttl_seconds=3600)
    stored = att_svc.save(project_id=project_id, data=_png(), declared_filename=None)

    adapter = _FakeAdapter()
    bridge = PMBridge(settings, adapter=adapter, registry=_registry(project_id, "codex"))
    monkeypatch.setattr(events_mod.hub, "publish", _noop_publish)

    async with sessionmaker() as db:
        await bridge.send(
            db, project_id=project_id, text="봐줘",
            attachments=[{"attachment_id": stored.attachment_id}], attachment_service=att_svc,
        )
    assert "첨부 이미지 파일 경로:" in adapter.submitted_text
    assert stored.abs_path in adapter.submitted_text


@pytest.mark.asyncio
async def test_send_partial_attachment_fails_whole(sessionmaker, tmp_path, monkeypatch):
    project_id = "PartProj"
    proj_root = tmp_path / project_id
    (proj_root / ".agiteam").mkdir(parents=True)
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", f'{{"{project_id}": "{proj_root}"}}')
    settings = Settings()
    att_svc = AttachmentService(proj_root, ttl_seconds=3600)
    stored = att_svc.save(project_id=project_id, data=_png(), declared_filename=None)

    adapter = _FakeAdapter()
    bridge = PMBridge(settings, adapter=adapter, registry=_registry(project_id, "claude_code"))
    monkeypatch.setattr(events_mod.hub, "publish", _noop_publish)

    from app.errors import WebguiError

    async with sessionmaker() as db:
        with pytest.raises(WebguiError) as ei:
            await bridge.send(
                db, project_id=project_id, text="봐줘",
                attachments=[
                    {"attachment_id": stored.attachment_id},
                    {"attachment_id": "att_" + "0" * 32},  # 존재하지 않음
                ],
                attachment_service=att_svc,
            )
    assert ei.value.code == "attachment_not_found"
    # 부분 송신 금지: adapter.submit 호출 안 됨
    assert adapter.submitted_text is None


@pytest.mark.asyncio
async def test_send_empty_text_with_attachment_ok(sessionmaker, tmp_path, monkeypatch):
    project_id = "EmptyTxt"
    proj_root = tmp_path / project_id
    (proj_root / ".agiteam").mkdir(parents=True)
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", f'{{"{project_id}": "{proj_root}"}}')
    settings = Settings()
    att_svc = AttachmentService(proj_root, ttl_seconds=3600)
    stored = att_svc.save(project_id=project_id, data=_png(), declared_filename=None)

    adapter = _FakeAdapter()
    bridge = PMBridge(settings, adapter=adapter, registry=_registry(project_id, "claude_code"))
    monkeypatch.setattr(events_mod.hub, "publish", _noop_publish)

    async with sessionmaker() as db:
        result = await bridge.send(
            db, project_id=project_id, text="",
            attachments=[{"attachment_id": stored.attachment_id}], attachment_service=att_svc,
        )
    assert "첨부 이미지를 확인하세요." in adapter.submitted_text
    assert result["ack"]["status"] == "sent"


async def _noop_publish(room_id, payload, project_id=None):
    return None
