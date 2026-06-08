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


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


class CmuxAdapter:
    def __init__(self, cmux_bin: str = "cmux", timeout: float = 15.0) -> None:
        self.cmux_bin = cmux_bin
        self.timeout = timeout

    def build_send_argv(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None = None,
    ) -> list[str]:
        if workspace_id:
            return [self.cmux_bin, "send", "--workspace", workspace_id, "--surface", surface_id, message]
        return [self.cmux_bin, "send", "--surface", surface_id, message]

    def build_send_key_argv(self, surface_id: str, workspace_id: str | None = None) -> list[str]:
        if workspace_id:
            return [self.cmux_bin, "send-key", "--workspace", workspace_id, "--surface", surface_id, "Enter"]
        return [self.cmux_bin, "send-key", "--surface", surface_id, "Enter"]

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

    async def submit(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        """send + send-key Enter atomic submit. DS-60 결과 schema 로 반환."""
        started = _now_iso()
        send = {"exit_code": None, "stdout": "", "stderr": "skipped"}
        send_key: dict[str, Any] = {"exit_code": None, "stdout": "", "stderr": "skipped"}
        if workspace_id:
            send = await self._rpc(
                "surface.send_text",
                {"workspace_id": workspace_id, "surface_id": surface_id, "text": message},
            )
            if send["exit_code"] == 0:
                send_key = await self._rpc(
                    "surface.send_key",
                    {"workspace_id": workspace_id, "surface_id": surface_id, "key": "Enter"},
                )
        else:
            send = await _run_with_caller_tty(
                _with_socket_arg(self.build_send_argv(surface_id, message, workspace_id)),
                self.timeout,
                tty=tty,
                env=_target_cmux_env(workspace_id, surface_id),
            )
        # send 성공 시에만 Enter 전송. (send 실패 시 제출 안 된 것으로 간주)
        if send["exit_code"] == 0 and send_key["exit_code"] is None:
            send_key = await _run_with_caller_tty(
                _with_socket_arg(self.build_send_key_argv(surface_id, workspace_id)),
                self.timeout,
                tty=tty,
                env=_target_cmux_env(workspace_id, surface_id),
            )
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
