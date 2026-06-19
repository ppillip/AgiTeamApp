"""cmux 어댑터 (DS-60 §5.3).

`cmux send` + `cmux send-key Enter` 를 한 단위(atomic)로 실행한다.
메시지는 shell interpolation 을 피하기 위해 subprocess argument 배열로 전달한다.
저수준 명령 실행만 담당하며 메시지 정책/권한/저장은 갖지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import re
import shlex
import select
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mux_port import MuxCapabilities, MuxPort


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# 멀티라인 안전전송 (웹 모니터 Shift+Enter 결함 대응).
# cmux send / surface.send_text 는 텍스트 내 \n·\r 을 Enter(제출)로 변환하므로
# 멀티라인을 그대로 보내면 첫 개행에서 잘려 여러 명령으로 쪼개진다.
#
# [채택안: send-key shift+enter] PM 실측 결과 `cmux send-key shift+enter` 가 수신
#   CLI(Claude Code·Codex) 양쪽 입력창에서 제출 없는 줄바꿈(soft newline)으로 동작함을
#   확인했다. 따라서 멀티라인은 개행으로 분해해 각 줄을 send 하고, 줄 사이마다
#   send-key shift+enter 로 줄바꿈을, 맨 끝에 send-key Enter 로 진짜 제출을 보낸다.
#   (폐기) bracketed-paste(ESC 래핑) A안: cmux send 가 raw ESC(0x1b)를 통과 안 시켜
#   마커가 입력창에 노출됨 → 전면 제거.
# 단일라인(개행 없음)은 회귀 0 보장을 위해 기존 단일 send + Enter 경로를 그대로 탄다.
_SOFT_NEWLINE_KEY = "shift+enter"  # 줄 사이 soft newline (제출 안 함)


def _normalize_lines(message: str) -> list[str]:
    """\\r\\n·\\r 을 \\n 으로 정규화한 뒤 줄 단위로 분해한다. 단일라인은 [message]."""
    normalized = message.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


_TREE_SURFACE_TTY_RE = re.compile(r"surface\s+(\S+).*?\stty=(\S+)")
_LAUNCH_SH_RE = re.compile(r"(/\S+?/\.agiteam/agents/[^/\s]+/launch\.sh)")
_ENV_TOKEN_RE = re.compile(
    r"(?:^|\s)("
    r"AGITEAM_PROJECT_ID|PROJECT_ID|"
    r"AGITEAM_TEAM_SESSION_ID|TEAM_SESSION_ID|"
    r"AGITEAM_AGENT_ID|AGENT_ID|"
    r"AGITEAM_ROLE|AGENT_ROLE|ROLE|"
    r"AGENT_CLI|AGENT_TYPE"
    r")=([^ \t\n]+)"
)


def _canonical_metadata(env: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    project_id = env.get("AGITEAM_PROJECT_ID") or env.get("PROJECT_ID")
    team_session_id = env.get("AGITEAM_TEAM_SESSION_ID") or env.get("TEAM_SESSION_ID")
    agent_id = env.get("AGITEAM_AGENT_ID") or env.get("AGENT_ID")
    role = env.get("AGITEAM_ROLE") or env.get("AGENT_ROLE") or env.get("ROLE")
    agent_type = env.get("AGENT_CLI") or env.get("AGENT_TYPE")
    if project_id:
        out["project_id"] = project_id
    if team_session_id:
        out["team_session_id"] = team_session_id
    if agent_id:
        out["agent_id"] = agent_id
    if role:
        out["role"] = role
    if agent_type:
        out["agent_type"] = agent_type
    return out


def _parse_launch_env_text(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("export "):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        for token in tokens[1:]:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            env[key] = value
    return _canonical_metadata(env)


def _parse_env_from_process_text(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in _ENV_TOKEN_RE.findall(text):
        env[key] = value.strip("'\"")
    return _canonical_metadata(env)


def _surface_ttys(tree_text: str) -> dict[str, str]:
    return {surface_id: tty for surface_id, tty in _TREE_SURFACE_TTY_RE.findall(tree_text)}


def _cmux_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("XPC_SERVICE_NAME", "XPC_FLAGS", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    home = env.get("HOME") or str(Path.home())
    env.setdefault("HOME", home)
    env.setdefault("CMUX_PORT", "9330")
    env.setdefault("CMUX_PORT_END", "9339")
    env.setdefault("CMUX_PORT_RANGE", "10")
    env.setdefault("CMUX_BUNDLE_ID", "com.cmuxterm.app")
    return env


def _with_socket_arg(argv: list[str]) -> list[str]:
    return argv


async def _run(argv: list[str], timeout: float, env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"exit_code": 124, "stdout": "", "stderr": "timeout"}
    return {
        "exit_code": proc.returncode,
        "stdout": (out or b"").decode("utf-8", "replace"),
        "stderr": (err or b"").decode("utf-8", "replace"),
    }


def _clean_script_output(text: str) -> str:
    return text.replace("\x04\b\b", "").replace("\r\n", "\n").replace("\r", "\n")


def _tree_from_session_snapshot() -> str:
    path = Path.home() / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""

    lines: list[str] = ["# cmux_session_snapshot_fallback", "window window:1 [current]"]
    workspace_index = 0
    for window in data.get("windows", []):
        manager = window.get("tabManager") or {}
        selected_index = manager.get("selectedWorkspaceIndex")
        for ws in manager.get("workspaces", []):
            workspace_index += 1
            workspace_id = ws.get("workspaceId") or f"workspace:{workspace_index}"
            title = ws.get("customTitle") or ws.get("title") or ws.get("currentDirectory") or workspace_id
            selected = " [selected] ◀ active" if selected_index == workspace_index - 1 else ""
            lines.append(f'├── workspace {workspace_id} "{title}"{selected}')
            for pane_index, panel in enumerate(ws.get("panels", []), start=1):
                if panel.get("type") != "terminal":
                    continue
                surface_id = panel.get("id")
                tty = panel.get("ttyName")
                surface_title = panel.get("customTitle") or panel.get("title")
                if not surface_id or not tty or not surface_title:
                    continue
                lines.append(f"│   ├── pane pane:{workspace_index}-{pane_index}")
                lines.append(f'│   │   └── surface {surface_id} [terminal] "{surface_title}" tty={tty}')
    return "\n".join(lines)


def _target_cmux_env(workspace_id: str | None = None, surface_id: str | None = None) -> dict[str, str]:
    env = _cmux_env()
    if workspace_id:
        env["CMUX_WORKSPACE_ID"] = workspace_id
    if surface_id:
        env["CMUX_SURFACE_ID"] = surface_id
    return env


def _run_with_pty_sync(argv: list[str], timeout: float, env: dict[str, str] | None = None) -> dict[str, Any]:
    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    output = bytearray()
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env or _cmux_env(),
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        while True:
            if time.monotonic() - started > timeout:
                proc.kill()
                proc.wait()
                return {"exit_code": 124, "stdout": output.decode("utf-8", "replace"), "stderr": "timeout"}
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
            if proc.poll() is not None:
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        return {
            "exit_code": proc.returncode,
            "stdout": _clean_script_output(output.decode("utf-8", "replace")),
            "stderr": "",
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


async def _run_with_pty(
    argv: list[str],
    timeout: float,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await asyncio.to_thread(_run_with_pty_sync, argv, timeout, env)


def _run_with_caller_tty_sync(
    argv: list[str],
    timeout: float,
    tty: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    tty_path = tty if tty.startswith("/dev/") else f"/dev/{tty}"
    fd = os.open(tty_path, os.O_RDWR | os.O_NOCTTY)

    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            argv,
            stdin=fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or _cmux_env(),
            close_fds=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return {
                "exit_code": 124,
                "stdout": (out or b"").decode("utf-8", "replace"),
                "stderr": "timeout",
            }
        return {
            "exit_code": proc.returncode,
            "stdout": (out or b"").decode("utf-8", "replace"),
            "stderr": (err or b"").decode("utf-8", "replace"),
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(fd)


async def _run_with_caller_tty(
    argv: list[str],
    timeout: float,
    tty: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if tty:
        return await asyncio.to_thread(_run_with_caller_tty_sync, argv, timeout, tty, env)
    return await _run_with_pty(argv, timeout, env)


async def _run_cmux(argv: list[str], timeout: float) -> dict[str, Any]:
    env = _cmux_env()
    argv = _with_socket_arg(argv)
    res = await _run(argv, timeout, env=env)
    if res["exit_code"] != 0:
        shell_res = await _run(["/bin/zsh", "-lc", shlex.join(argv)], timeout, env=env)
        if shell_res["exit_code"] == 0 or shell_res["stdout"].strip():
            return shell_res
    combined = f"{res['stdout']}\n{res['stderr']}"
    if res["exit_code"] != 0 and (
        "Broken pipe" in combined or "Failed to write to socket" in combined
    ):
        return await _run_with_pty(argv, timeout)
    return res


def _proxy_rpc_sync(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = os.environ.get("CMUX_BRIDGE_PROXY_URL", "http://127.0.0.1:8765/rpc")
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local opt-in proxy only
            text = resp.read().decode("utf-8", "replace")
            return {"exit_code": 0, "stdout": text, "stderr": ""}
    except (OSError, urllib.error.URLError) as exc:
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


async def _proxy_rpc(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    return await asyncio.to_thread(_proxy_rpc_sync, method, params, timeout)


class CmuxAdapter(MuxPort):
    """MuxPort 의 cmux 구현체 (DS-70 / MX-20).

    현 동작 그대로 유지한다(절대경로 cmux 호출, argv 구성 동일). cmux 회귀 0.
    """

    mux_name = "cmux"

    def __init__(self, cmux_bin: str = "cmux", timeout: float = 15.0) -> None:
        self.cmux_bin = cmux_bin
        self.timeout = timeout

    def capabilities(self) -> MuxCapabilities:
        """cmux 기능 플래그 (DS-70 §5.1)."""
        return MuxCapabilities(
            mux="cmux",
            send_text=True,
            send_key=True,
            read_screen=True,
            watch_stream=True,   # events/hooks 또는 read polling 기반
            events=True,
            hooks=True,
            list_surfaces=True,
            open_surface=True,
            label_surface=True,
            label_color=True,
            browser_control=True,  # cmux 전용
        )

    def build_send_argv(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None = None,
    ) -> list[str]:
        # 한 줄(개행 없음)을 보낸다. 멀티라인 분해는 submit() 가 담당한다.
        if workspace_id:
            return [self.cmux_bin, "send", "--workspace", workspace_id, "--surface", surface_id, message]
        return [self.cmux_bin, "send", "--surface", surface_id, message]

    def build_send_key_argv(
        self,
        surface_id: str,
        workspace_id: str | None = None,
        key: str = "Enter",
    ) -> list[str]:
        if workspace_id:
            return [self.cmux_bin, "send-key", "--workspace", workspace_id, "--surface", surface_id, key]
        return [self.cmux_bin, "send-key", "--surface", surface_id, key]

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        res = await _run_cmux(
            [self.cmux_bin, "rpc", method, json.dumps(params, ensure_ascii=False)],
            self.timeout,
        )
        combined = f"{res['stdout']}\n{res['stderr']}"
        if res["exit_code"] == 0 and "Failed to write to socket" not in combined and "Broken pipe" not in combined:
            return res
        return await _proxy_rpc(method, params, self.timeout)

    async def tree(self) -> str:
        """`cmux tree --all` 출력(stdout). 실패 시 빈 문자열."""
        res = await _run_cmux([self.cmux_bin, "tree", "--all"], self.timeout)
        stdout = "" if "Failed to write to socket" in res["stdout"] else res["stdout"]
        if stdout.strip():
            return stdout
        if res["exit_code"] != 0:
            fallback = _tree_from_session_snapshot()
            if fallback:
                return fallback
            logger.warning(
                "cmux tree failed exit_code=%s stderr=%s",
                res["exit_code"],
                res["stderr"][:500],
            )
            return ""
        return res["stdout"]

    async def runtime_metadata(self, tree_text: str) -> dict[str, dict[str, str]]:
        """surface tty 의 실행 프로세스/launch.sh 에서 AgiTeam 런타임 키만 추출한다.

        cmux workspace 제목은 사람이 붙인 팀명일 수 있으므로 project_id 의 SSoT 가 아니다.
        launch.sh 또는 프로세스 env 의 PROJECT_ID/TEAM_SESSION_ID/AGENT_ID 를 우선 사용한다.
        """
        async def one(surface_id: str, tty: str) -> tuple[str, dict[str, str] | None]:
            res = await _run(["ps", "-t", tty, "-wwE", "-o", "command"], self.timeout)
            if res["exit_code"] != 0:
                return surface_id, None
            ps_text = res["stdout"]
            launch_match = _LAUNCH_SH_RE.search(ps_text)
            if launch_match:
                path = Path(launch_match.group(1))
                try:
                    launch_meta = _parse_launch_env_text(path.read_text(encoding="utf-8"))
                except OSError:
                    launch_meta = {}
                # DV-49/QI-WG-027: project_id = 실재 root 폴더명. launch.sh 경로
                # (/<ROOT>/.agiteam/agents/<role>/launch.sh)의 <ROOT> basename 을 SSoT 로
                # 사용해 env 의 오타/쓰레기(AGITEAM_PROJECT_ID="2"/"AGI개발팀")·cmux title 을 덮어쓴다.
                parts = str(path).split("/.agiteam/", 1)
                if len(parts) == 2 and parts[0]:
                    root_name = Path(parts[0]).name
                    if root_name:
                        launch_meta = dict(launch_meta)
                        launch_meta["project_id"] = root_name
                if launch_meta:
                    return surface_id, launch_meta
            proc_meta = _parse_env_from_process_text(ps_text)
            if proc_meta:
                return surface_id, proc_meta
            return surface_id, None

        pairs = await asyncio.gather(
            *(one(surface_id, tty) for surface_id, tty in _surface_ttys(tree_text).items())
        )
        metadata: dict[str, dict[str, str]] = {}
        for surface_id, meta in pairs:
            if meta:
                metadata[surface_id] = meta
        return metadata

    async def read_screen(
        self,
        surface_id: str,
        lines: int = 40,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        """`cmux read-screen` 결과."""
        argv = [self.cmux_bin, "read-screen", "--surface", surface_id, "--lines", str(lines)]
        if workspace_id:
            argv = [
                self.cmux_bin,
                "read-screen",
                "--workspace",
                workspace_id,
                "--surface",
                surface_id,
                "--lines",
                str(lines),
            ]
            rpc = await self._rpc(
                "surface.read_text",
                {"workspace_id": workspace_id, "surface_id": surface_id, "lines": lines},
            )
            if rpc["exit_code"] == 0:
                try:
                    data = json.loads(rpc["stdout"])
                except ValueError:
                    data = {}
                text = data.get("text")
                if isinstance(text, str):
                    return {"exit_code": 0, "stdout": text, "stderr": "", "rpc": data}
        return await _run_with_caller_tty(
            _with_socket_arg(argv),
            self.timeout,
            tty=tty,
            env=_target_cmux_env(workspace_id, surface_id),
        )

    async def ping(
        self,
        surface_id: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> bool:
        """송신 직전 liveness 확정용 read-screen 핑 (DS-60 liveness 확정안)."""
        res = await self.read_screen(surface_id, lines=1, workspace_id=workspace_id, tty=tty)
        return res["exit_code"] == 0

    async def _send_text_once(
        self,
        surface_id: str,
        text: str,
        workspace_id: str | None,
        tty: str | None,
    ) -> dict[str, Any]:
        """한 줄 텍스트 전송 (workspace 있으면 RPC, 없으면 caller-tty CLI)."""
        if workspace_id:
            return await self._rpc(
                "surface.send_text",
                {"workspace_id": workspace_id, "surface_id": surface_id, "text": text},
            )
        return await _run_with_caller_tty(
            _with_socket_arg(self.build_send_argv(surface_id, text, workspace_id)),
            self.timeout,
            tty=tty,
            env=_target_cmux_env(workspace_id, surface_id),
        )

    async def _send_key_once(
        self,
        surface_id: str,
        key: str,
        workspace_id: str | None,
        tty: str | None,
    ) -> dict[str, Any]:
        """키 이벤트 전송 (Enter / shift+enter 등)."""
        if workspace_id:
            return await self._rpc(
                "surface.send_key",
                {"workspace_id": workspace_id, "surface_id": surface_id, "key": key},
            )
        return await _run_with_caller_tty(
            _with_socket_arg(self.build_send_key_argv(surface_id, workspace_id, key)),
            self.timeout,
            tty=tty,
            env=_target_cmux_env(workspace_id, surface_id),
        )

    async def submit(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        """안전 제출. 단일라인은 send + send-key Enter(기존 동작 그대로, 회귀 0).

        멀티라인은 개행으로 분해해 각 줄을 send 하고, 줄 사이마다 send-key shift+enter
        (제출 없는 줄바꿈)를, 맨 끝에 send-key Enter(진짜 제출)를 보낸다.
        """
        started = _now_iso()
        lines = _normalize_lines(message)
        if len(lines) <= 1:
            return await self._submit_single(surface_id, message, workspace_id, tty, started)
        return await self._submit_multiline(surface_id, lines, workspace_id, tty, started)

    async def _submit_single(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None,
        tty: str | None,
        started: str,
    ) -> dict[str, Any]:
        """단일라인 제출 — 기존 동작 보존(send 성공 시에만 Enter)."""
        send_key: dict[str, Any] = {"exit_code": None, "stdout": "", "stderr": "skipped"}
        send = await self._send_text_once(surface_id, message, workspace_id, tty)
        if send["exit_code"] == 0:
            send_key = await self._send_key_once(surface_id, "Enter", workspace_id, tty)
        ended = _now_iso()
        submitted = send["exit_code"] == 0 and send_key["exit_code"] == 0
        return {
            "surface_id": surface_id,
            "workspace_id": workspace_id,
            "tty": tty,
            "send": send,
            "send_key": send_key,
            "submitted": submitted,
            "started_at": started,
            "ended_at": ended,
        }

    async def _submit_multiline(
        self,
        surface_id: str,
        lines: list[str],
        workspace_id: str | None,
        tty: str | None,
        started: str,
    ) -> dict[str, Any]:
        """멀티라인 제출 — 줄 사이 shift+enter, 끝에 Enter."""
        steps: list[dict[str, Any]] = []
        last_send: dict[str, Any] = {"exit_code": None, "stdout": "", "stderr": "skipped"}
        send_key: dict[str, Any] = {"exit_code": None, "stdout": "", "stderr": "skipped"}
        ok = True
        for index, line in enumerate(lines):
            if index > 0:
                # 줄 사이: soft newline (제출 안 함)
                nl = await self._send_key_once(surface_id, _SOFT_NEWLINE_KEY, workspace_id, tty)
                steps.append({"send_key": nl})
                if nl["exit_code"] != 0:
                    ok = False
                    break
            if line == "":
                # 빈 줄은 위 shift+enter 로 이미 줄바꿈됨. cmux send "" 호출 회피.
                continue
            s = await self._send_text_once(surface_id, line, workspace_id, tty)
            last_send = s
            steps.append({"send": s})
            if s["exit_code"] != 0:
                ok = False
                break
        if ok:
            send_key = await self._send_key_once(surface_id, "Enter", workspace_id, tty)
            steps.append({"send_key": send_key})
        ended = _now_iso()
        submitted = ok and send_key.get("exit_code") == 0
        return {
            "surface_id": surface_id,
            "workspace_id": workspace_id,
            "tty": tty,
            "send": last_send,
            "send_key": send_key,
            "submitted": submitted,
            "steps": steps,
            "started_at": started,
            "ended_at": ended,
        }
