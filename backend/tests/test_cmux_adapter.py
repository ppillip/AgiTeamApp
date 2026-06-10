"""cmux 어댑터 테스트 (DS-60 §5.3).

가짜 cmux 실행 스크립트로 send + send-key Enter atomic 동작을 검증한다.
shell interpolation 회피(arg 배열) 도 함께 점검.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.services.cmux_adapter import (
    _SOFT_NEWLINE_KEY,
    CmuxAdapter,
    _normalize_lines,
    _parse_env_from_process_text,
    _parse_launch_env_text,
)


def _make_fake_cmux(tmp_path: Path, exit_code: int = 0) -> str:
    log = tmp_path / "calls.log"
    script = tmp_path / "cmux"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def test_build_argv_uses_array_no_shell():
    a = CmuxAdapter("cmux")
    argv = a.build_send_argv("surface:01", "rm -rf /; echo hacked")
    assert argv == ["cmux", "send", "--surface", "surface:01", "rm -rf /; echo hacked"]
    assert a.build_send_key_argv("surface:01") == ["cmux", "send-key", "--surface", "surface:01", "Enter"]


def test_normalize_lines_singleline():
    # 개행 없으면 한 줄짜리 리스트(단일라인 판정 근거).
    assert _normalize_lines("hello world") == ["hello world"]
    assert _normalize_lines("") == [""]


def test_normalize_lines_splits_and_normalizes_crlf():
    assert _normalize_lines("line1\nline2\nline3") == ["line1", "line2", "line3"]
    assert _normalize_lines("a\r\nb") == ["a", "b"]
    assert _normalize_lines("a\rb") == ["a", "b"]


def test_soft_newline_key_is_shift_enter():
    assert _SOFT_NEWLINE_KEY == "shift+enter"


def test_build_send_argv_passes_line_literally():
    # build_send_argv 는 한 줄을 그대로 전달(변환·래핑 없음).
    a = CmuxAdapter("cmux")
    assert a.build_send_argv("surface:01", "line1") == [
        "cmux", "send", "--surface", "surface:01", "line1",
    ]


def test_build_send_key_argv_accepts_custom_key():
    a = CmuxAdapter("cmux")
    # 기본은 Enter(회귀 0)
    assert a.build_send_key_argv("surface:01") == [
        "cmux", "send-key", "--surface", "surface:01", "Enter",
    ]
    # shift+enter 도 구성 가능
    assert a.build_send_key_argv("surface:01", None, "shift+enter") == [
        "cmux", "send-key", "--surface", "surface:01", "shift+enter",
    ]
    assert a.build_send_key_argv("surface:01", "workspace:40", "shift+enter") == [
        "cmux", "send-key", "--workspace", "workspace:40", "--surface", "surface:01", "shift+enter",
    ]


@pytest.mark.asyncio
async def test_submit_multiline_uses_shift_enter_between_lines(tmp_path):
    cmux = _make_fake_cmux(tmp_path, exit_code=0)
    a = CmuxAdapter(cmux, timeout=5)
    res = await a.submit("surface:01", "first line\nsecond line")
    assert res["submitted"] is True
    log = (tmp_path / "calls.log").read_text(encoding="utf-8")
    lines = [ln for ln in log.splitlines() if ln.strip()]
    # 순서: send first line → send-key shift+enter → send second line → send-key Enter
    assert lines == [
        "send --surface surface:01 first line",
        "send-key --surface surface:01 shift+enter",
        "send --surface surface:01 second line",
        "send-key --surface surface:01 Enter",
    ]


@pytest.mark.asyncio
async def test_submit_multiline_three_lines_two_soft_newlines(tmp_path):
    cmux = _make_fake_cmux(tmp_path, exit_code=0)
    a = CmuxAdapter(cmux, timeout=5)
    res = await a.submit("surface:01", "a\nb\nc")
    assert res["submitted"] is True
    log = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert log.count("shift+enter") == 2  # 줄 사이 2회
    assert log.count("send-key --surface surface:01 Enter") == 1  # 끝 제출 1회


def test_build_argv_can_scope_workspace():
    a = CmuxAdapter("cmux")
    assert a.build_send_argv("surface:01", "ping", "workspace:40") == [
        "cmux",
        "send",
        "--workspace",
        "workspace:40",
        "--surface",
        "surface:01",
        "ping",
    ]
    assert a.build_send_key_argv("surface:01", "workspace:40") == [
        "cmux",
        "send-key",
        "--workspace",
        "workspace:40",
        "--surface",
        "surface:01",
        "Enter",
    ]


def test_parse_launch_env_text_canonicalizes_agiteam_keys():
    text = """
export PROJECT_ID='HookTest'
export TEAM_SESSION_ID='20260608_121158'
export AGENT_ID='DeveloperBE'
export AGENT_CLI='claude'
"""
    assert _parse_launch_env_text(text) == {
        "project_id": "HookTest",
        "team_session_id": "20260608_121158",
        "agent_id": "DeveloperBE",
        "agent_type": "claude",
    }


def test_parse_process_env_text_for_direct_pm_launch():
    text = (
        "COMMAND\n"
        "PROJECT_ID=HookTest TEAM_SESSION_ID=20260608_121158 "
        "AGENT_ID=PM AGENT_CLI=claude claude --dangerously-skip-permissions"
    )
    assert _parse_env_from_process_text(text) == {
        "project_id": "HookTest",
        "team_session_id": "20260608_121158",
        "agent_id": "PM",
        "agent_type": "claude",
    }


@pytest.mark.asyncio
async def test_submit_success(tmp_path):
    cmux = _make_fake_cmux(tmp_path, exit_code=0)
    a = CmuxAdapter(cmux, timeout=5)
    res = await a.submit("surface:01", "작업 지시")
    assert res["submitted"] is True
    assert res["send"]["exit_code"] == 0
    assert res["send_key"]["exit_code"] == 0
    log = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "send --surface surface:01 작업 지시" in log
    assert "send-key --surface surface:01 Enter" in log


@pytest.mark.asyncio
async def test_submit_success_with_workspace_scope(tmp_path):
    cmux = _make_fake_cmux(tmp_path, exit_code=0)
    a = CmuxAdapter(cmux, timeout=5)
    res = await a.submit("surface:01", "작업 지시", "workspace:40")
    assert res["submitted"] is True
    assert res["workspace_id"] == "workspace:40"
    log = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert 'rpc surface.send_text {"workspace_id": "workspace:40", "surface_id": "surface:01"' in log
    assert '"text": "작업 지시"' in log
    assert 'rpc surface.send_key {"workspace_id": "workspace:40", "surface_id": "surface:01"' in log
    assert '"key": "Enter"' in log


@pytest.mark.asyncio
async def test_submit_failure_skips_enter(tmp_path):
    cmux = _make_fake_cmux(tmp_path, exit_code=1)
    a = CmuxAdapter(cmux, timeout=5)
    res = await a.submit("surface:01", "x")
    assert res["submitted"] is False
    assert res["send"]["exit_code"] == 1
    # send 실패 시 Enter 는 전송하지 않음
    assert res["send_key"]["stderr"] == "skipped"
