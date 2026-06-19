"""tmux 어댑터 **skeleton** (DS-70 / MX-20).

[상태] 미구현 / 미테스트. 이번 MX-20 범위는 cmux 추상화이며, tmux 는 자리(skeleton)와
구현 방안 주석만 둔다(엔진 bash `team` CLI 의 tmux 방침과 동일). 각 메서드는
``NotImplementedError`` 를 던지며, 활성화 시 아래 주석의 tmux 명령으로 구현한다.

대응 매핑(DS-70 §6 cmux/tmux 어댑터 매핑표 요약):
- 중립 용어:  workspace=session, tab=window, surface=pane, title=pane title/window name
- 관찰 스트림: cmux events/hooks → tmux ``pipe-pane``/``capture-pane`` 폴백
"""
from __future__ import annotations

from typing import Any

from .mux_port import MuxCapabilities, MuxPort


class TmuxAdapter(MuxPort):
    """MuxPort 의 tmux 구현 자리. 전부 미구현(NotImplemented)."""

    mux_name = "tmux"

    def __init__(self, tmux_bin: str = "tmux", timeout: float = 15.0) -> None:
        self.tmux_bin = tmux_bin
        self.timeout = timeout

    def capabilities(self) -> MuxCapabilities:
        # tmux 는 events/hooks/색상 라벨이 없고 watch 는 pipe-pane 으로 가능.
        # open/label 은 부분 지원(best-effort). 실제 동작은 미구현이므로 참고용 플래그.
        return MuxCapabilities(
            mux="tmux",
            send_text=True,
            send_key=True,
            read_screen=True,
            watch_stream=True,   # tmux: pipe-pane -o <sink>
            events=False,        # tmux: 네이티브 이벤트 없음
            hooks=False,
            list_surfaces=True,
            open_surface=True,
            label_surface=True,  # tmux: rename-window / select-pane -T (부분)
            label_color=False,   # tmux: native 색상 라벨 없음 → 모니터 registry 보강
            browser_control=False,
        )

    async def tree(self) -> str:
        # tmux: `tmux list-panes -a -F '#{session_name} #{window_id} #{pane_id} #{pane_title} #{pane_tty} #{pane_pid}'`
        #       + `tmux list-sessions` 로 cross-session 인벤토리 구성 후 cmux tree 와 동형 텍스트로 정규화.
        raise NotImplementedError("TmuxAdapter.tree (MX-20 미구현 skeleton)")

    async def runtime_metadata(self, tree_text: str) -> dict[str, dict[str, str]]:
        # tmux: pane_tty 로 `ps -t <tty> -wwE -o command` (cmux 와 동일) → launch.sh/env 메타 추출.
        #       pane_pid 자식 프로세스 추적으로 AGENT_ROLE/PROJECT_ID/TEAM_SESSION_ID 를 권위값으로.
        raise NotImplementedError("TmuxAdapter.runtime_metadata (MX-20 미구현 skeleton)")

    async def read_screen(
        self,
        surface_id: str,
        lines: int = 40,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        # tmux: `tmux capture-pane -t <pane> -p -S -<lines>` (스크롤백 lines 캡처) → ANSI/프롬프트 후처리.
        raise NotImplementedError("TmuxAdapter.read_screen (MX-20 미구현 skeleton)")

    async def ping(
        self,
        surface_id: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> bool:
        # tmux: `tmux capture-pane -t <pane> -p -S -1` exit_code 0 여부로 liveness 확정.
        raise NotImplementedError("TmuxAdapter.ping (MX-20 미구현 skeleton)")

    async def submit(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        # tmux: `tmux send-keys -t <pane> -l <text>` (리터럴 입력) + `tmux send-keys -t <pane> Enter` (제출)
        #       을 한 단위(atomic)로 실행. cmux submit 과 동일한 결과 schema 로 반환.
        raise NotImplementedError("TmuxAdapter.submit (MX-20 미구현 skeleton)")
