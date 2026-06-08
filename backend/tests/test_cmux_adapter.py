"""cmux 어댑터 테스트 (DS-60 §5.3).

가짜 cmux 실행 스크립트로 send + send-key Enter atomic 동작을 검증한다.
shell interpolation 회피(arg 배열) 도 함께 점검.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.services.cmux_adapter import CmuxAdapter, _parse_env_from_process_text, _parse_launch_env_text


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
