"""멀티플렉서 포트/어댑터 추상화 (DS-70 / MX-20).

엔진(`system/AgiTeamBuilder/AgiTeam/`)의 bash `team` CLI와 **같은 계약, 다른 구현**이다.
모니터(AgiTeamApp) backend 는 서버 프로세스이며 cmux 를 절대경로로 직접 호출하고,
cross-project(`cmux tree --all`)로 전 프로젝트를 발견하므로 bash team CLI 를 shell-out
하지 않고 자체 Python 어댑터(`CmuxAdapter`/`TmuxAdapter`)로 포트를 구현한다.

구조:
- ``MuxPort`` — 추상 인터페이스. backend 가 실제 호출하는 공개 메서드의 계약만 정의.
- ``CmuxAdapter`` (cmux_adapter.py) — 현 동작 그대로의 cmux 구현체. **cmux 회귀 0**.
- ``TmuxAdapter`` (tmux_adapter.py) — skeleton(NotImplemented + 구현방안 주석). 이번 미구현.
- ``get_mux_adapter()`` — transport.mux 로 어댑터를 선택하는 팩토리. 현 유효 mux 는 cmux 만.

설계 메모(MX-20 범위 가드):
- backend 가 실제로 사용하는 포트 메서드는 관찰/송신 중심
  (``tree``/``runtime_metadata``/``read_screen``/``ping``/``submit``)이다.
  surface 생성/소멸/장식(open/close/label)은 엔진 런타임(agiteam.sh) 책임이라
  backend 포트에는 포함하지 않는다(필요 시 후속 확장).
- ``build_send_argv``/``build_send_key_argv`` 는 cmux 전용 argv 구성 세부이므로
  포트가 아니라 ``CmuxAdapter`` 에만 둔다(기존 단위테스트 계약 유지).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 순환 import 방지 — 타입 힌트 전용
    from ..config import Settings


@dataclass(frozen=True)
class MuxCapabilities:
    """어댑터별 기능 플래그 (DS-70 §5). graceful degradation 판단 근거."""

    mux: str
    send_text: bool = False
    send_key: bool = False
    read_screen: bool = False
    watch_stream: bool = False
    events: bool = False
    hooks: bool = False
    list_surfaces: bool = False
    open_surface: bool = False
    label_surface: bool = False
    label_color: bool = False
    browser_control: bool = False


class MuxPort(ABC):
    """터미널 멀티플렉서 추상 포트.

    backend 가 호출하는 모든 멀티플렉서 연산은 이 인터페이스를 경유한다.
    호출자는 cmux/tmux 의 native 명령을 직접 알지 않는다.
    """

    mux_name: str = "mux"

    @abstractmethod
    def capabilities(self) -> MuxCapabilities:
        """이 어댑터가 지원하는 기능 플래그."""

    @abstractmethod
    async def tree(self) -> str:
        """전 프로젝트 surface 트리(cross-project 발견). 실패 시 빈 문자열."""

    @abstractmethod
    async def runtime_metadata(self, tree_text: str) -> dict[str, dict[str, str]]:
        """surface tty 의 실행 프로세스/launch.sh 에서 AgiTeam 런타임 키 추출."""

    @abstractmethod
    async def read_screen(
        self,
        surface_id: str,
        lines: int = 40,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        """특정 surface 화면 1회 캡처."""

    @abstractmethod
    async def ping(
        self,
        surface_id: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> bool:
        """송신 직전 liveness 확정용 핑."""

    @abstractmethod
    async def submit(
        self,
        surface_id: str,
        message: str,
        workspace_id: str | None = None,
        tty: str | None = None,
    ) -> dict[str, Any]:
        """텍스트 입력 + 제출(Enter)을 한 단위(atomic)로 실행."""


# 현 시점 유효한 mux 식별자 (tmux 는 미지원 가드 대상)
SUPPORTED_MUX = ("cmux",)


def resolve_project_mux(
    settings: "Settings",
    project_id: str | None = None,
    *,
    project_mux: str | None = None,
) -> str:
    """프로젝트별 유효 mux 를 해소한다.

    우선순위: 명시 ``project_mux`` > (프로젝트 agiteam.json transport.mux) > ``settings.mux``.
    cross-project 발견(tree --all)은 항상 기본 ``settings.mux``(cmux)를 사용한다.

    [MX-20 범위] 현 시점 backend send/read 경로는 항상 기본 mux(cmux)로 동작한다.
    프로젝트별 agiteam.json transport.mux 의 실제 파일 읽기는 tmux 가 도입될 때
    이 함수에 주입한다(아래 ``project_mux`` 인자로 호출부가 전달). 지금은
    settings 기반으로만 해소해 **기존 동작과 동일**(cmux)을 보장한다.
    """
    chosen = project_mux or settings.mux or "cmux"
    return chosen


def get_mux_adapter(
    settings: "Settings",
    mux: str | None = None,
) -> MuxPort:
    """transport.mux 로 어댑터를 선택하는 팩토리.

    - ``mux`` 미지정 시 ``settings.mux``(기본 cmux) 사용.
    - cmux: 기존과 동일하게 ``CmuxAdapter(cmux_bin, cmux_timeout_seconds)`` 반환 → 회귀 0.
    - tmux 등 미지원 mux: 명확히 거절(가드). skeleton 은 존재하나 실동작 미지원.
    """
    chosen = (mux or settings.mux or "cmux").strip().lower()
    if chosen == "cmux":
        # 지연 import 로 순환 의존 회피 (cmux_adapter 가 mux_port 를 import)
        from .cmux_adapter import CmuxAdapter

        return CmuxAdapter(settings.cmux_bin, settings.cmux_timeout_seconds)
    if chosen == "tmux":
        raise ValueError(
            "tmux transport 는 아직 미지원입니다(MX-20). TmuxAdapter 는 skeleton 만 존재합니다."
        )
    raise ValueError(f"알 수 없는 mux transport: {chosen!r} (유효: {SUPPORTED_MUX})")
