"""MX-20: MuxPort 추상화 — 팩토리/capabilities/tmux skeleton 가드 검증.

cmux 실동작 회귀는 기존 test_cmux_adapter 가 커버한다. 본 파일은 포트/팩토리 계약만 본다.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.cmux_adapter import CmuxAdapter
from app.services.mux_port import (
    SUPPORTED_MUX,
    MuxCapabilities,
    MuxPort,
    get_mux_adapter,
    resolve_project_mux,
)
from app.services.tmux_adapter import TmuxAdapter


def test_adapters_implement_port():
    assert issubclass(CmuxAdapter, MuxPort)
    assert issubclass(TmuxAdapter, MuxPort)


def test_factory_default_returns_cmux():
    s = Settings()
    a = get_mux_adapter(s)
    assert isinstance(a, CmuxAdapter)
    assert a.mux_name == "cmux"
    # 기존 동작 동일: 절대경로 cmux_bin/timeout 주입
    assert a.cmux_bin == s.cmux_bin
    assert a.timeout == s.cmux_timeout_seconds


def test_factory_explicit_cmux():
    a = get_mux_adapter(Settings(), mux="cmux")
    assert isinstance(a, CmuxAdapter)


def test_factory_tmux_is_guarded():
    with pytest.raises(ValueError, match="tmux"):
        get_mux_adapter(Settings(), mux="tmux")


def test_factory_unknown_mux_rejected():
    with pytest.raises(ValueError):
        get_mux_adapter(Settings(), mux="screen")


def test_cmux_capabilities():
    caps = CmuxAdapter("cmux").capabilities()
    assert isinstance(caps, MuxCapabilities)
    assert caps.mux == "cmux"
    assert caps.events and caps.hooks and caps.browser_control
    assert caps.send_text and caps.read_screen


def test_tmux_capabilities_flags():
    caps = TmuxAdapter().capabilities()
    assert caps.mux == "tmux"
    # tmux 는 네이티브 events/hooks/색상라벨 없음
    assert caps.events is False and caps.hooks is False and caps.label_color is False
    assert caps.watch_stream is True  # pipe-pane 으로 가능


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [
    ("tree", ()),
    ("runtime_metadata", ("",)),
    ("read_screen", ("surface:01",)),
    ("ping", ("surface:01",)),
    ("submit", ("surface:01", "hi")),
])
async def test_tmux_skeleton_raises_not_implemented(method, args):
    a = TmuxAdapter()
    with pytest.raises(NotImplementedError):
        await getattr(a, method)(*args)


def test_resolve_project_mux_defaults_to_settings():
    s = Settings()
    assert resolve_project_mux(s) == "cmux"
    assert resolve_project_mux(s, "AnyProject") == "cmux"


def test_resolve_project_mux_explicit_override():
    s = Settings()
    assert resolve_project_mux(s, "P", project_mux="tmux") == "tmux"


def test_supported_mux_is_cmux_only():
    assert SUPPORTED_MUX == ("cmux",)
