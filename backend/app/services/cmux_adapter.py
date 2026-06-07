"""cmux 어댑터 (DS-60 §5.3).

`cmux send` + `cmux send-key Enter` 를 한 단위(atomic)로 실행한다.
메시지는 shell interpolation 을 피하기 위해 subprocess argument 배열로 전달한다.
저수준 명령 실행만 담당하며 메시지 정책/권한/저장은 갖지 않는다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _run(argv: list[str], timeout: float) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


class CmuxAdapter:
    def __init__(self, cmux_bin: str = "cmux", timeout: float = 15.0) -> None:
        self.cmux_bin = cmux_bin
        self.timeout = timeout

    def build_send_argv(self, surface_id: str, message: str) -> list[str]:
        return [self.cmux_bin, "send", "--surface", surface_id, message]

    def build_send_key_argv(self, surface_id: str) -> list[str]:
        return [self.cmux_bin, "send-key", "--surface", surface_id, "Enter"]

    async def tree(self) -> str:
        """`cmux tree --all` 출력(stdout). 실패 시 빈 문자열."""
        res = await _run([self.cmux_bin, "tree", "--all"], self.timeout)
        return res["stdout"] if res["exit_code"] == 0 else ""

    async def read_screen(self, surface_id: str, lines: int = 40) -> dict[str, Any]:
        """`cmux read-screen` 결과."""
        return await _run(
            [self.cmux_bin, "read-screen", "--surface", surface_id, "--lines", str(lines)],
            self.timeout,
        )

    async def ping(self, surface_id: str) -> bool:
        """송신 직전 liveness 확정용 read-screen 핑 (DS-60 liveness 확정안)."""
        res = await self.read_screen(surface_id, lines=1)
        return res["exit_code"] == 0

    async def submit(self, surface_id: str, message: str) -> dict[str, Any]:
        """send + send-key Enter atomic submit. DS-60 결과 schema 로 반환."""
        started = _now_iso()
        send = await _run(self.build_send_argv(surface_id, message), self.timeout)
        send_key: dict[str, Any] = {"exit_code": None, "stdout": "", "stderr": "skipped"}
        # send 성공 시에만 Enter 전송. (send 실패 시 제출 안 된 것으로 간주)
        if send["exit_code"] == 0:
            send_key = await _run(self.build_send_key_argv(surface_id), self.timeout)
        ended = _now_iso()
        submitted = send["exit_code"] == 0 and send_key["exit_code"] == 0
        return {
            "surface_id": surface_id,
            "send": send,
            "send_key": send_key,
            "submitted": submitted,
            "started_at": started,
            "ended_at": ended,
        }
