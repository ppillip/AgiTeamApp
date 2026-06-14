"""애플리케이션 설정 (DS-60 §DV-50 설정 인계).

환경변수 prefix: WEBGUI_
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    """config.py 위치: <repo>/system/AgiTeamApp/backend/app/config.py → parents[4] == <repo>."""
    return Path(__file__).resolve().parents[4]


# 산출물 트리 root_type allowlist (코드탭/페르소나탭 추가, 제우스 2026-06-14).
# documents = 산출물 문서 트리(현행), system = 코드(소스) 트리, persona = brain(역할 페르소나) 트리.
# 그 외 값은 router 에서 거절. 신규 탭 추가는 이 매핑에 한 줄만 더하면 전 분기에 자동 반영된다.
# (모듈 레벨 상수 — pydantic BaseSettings 의 언더스코어 PrivateAttr 가로채기를 피한다.)
ROOT_TYPE_SUBDIR: dict[str, str] = {"documents": "documents", "system": "system", "persona": "brain"}


def _root_type_subdir(root_type: str | None) -> str:
    """root_type → 프로젝트 루트 하위 디렉터리명. 미지정/빈값/미지의 값 = documents(하위호환).

    엄격한 enum 거절은 router 레벨(invalid_request)에서 수행한다. 여기서는 안전한
    기본값(documents)으로 수렴시켜, 잘못된 값이 와도 절대 allowlist 외 임의경로로 새지 않게 한다.
    """
    rt = (root_type or "").strip().lower()
    return ROOT_TYPE_SUBDIR.get(rt, "documents")


def _default_artifacts_root() -> Path:
    """레포 기준 documents/products/AgiTeamApp 자동 추정."""
    return _repo_root() / "documents" / "products" / "AgiTeamApp"


def _default_projects_base() -> Path:
    """프로젝트들이 모여있는 상위 디렉터리(예: ~/Projects). 기본 = <repo>의 부모."""
    return _repo_root().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEBGUI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 저장소
    database_url: str = "postgresql+psycopg://webgui:webgui@localhost:5432/webgui"

    # 모니터 대상 프로젝트
    project_id: str = "AgiTeamApp"

    # 산출물 allowlist 루트 (DS-20 §13.1 / DS-60 §11.2)
    artifacts_root: Path = Field(default_factory=_default_artifacts_root)

    # 인증 (DS-40 §3.2). 미설정이면 로컬 dev 모드(인증 생략).
    api_token: str | None = None
    collector_token: str | None = None

    # CORS. 기본은 localhost dev 서버만 허용한다. IP 접근 공개 구동 시
    # WEBGUI_CORS_ALLOW_ORIGINS 또는 WEBGUI_CORS_ALLOW_ORIGIN_REGEX 로 명시 확장한다.
    cors_allow_origins: str = "http://localhost:1420,http://127.0.0.1:1420"
    cors_allow_origin_regex: str | None = None

    # cmux 연동 (DS-60 §5.3). PATH 의존 금지 — 절대경로 기본값 (제우스 2026-06-07).
    cmux_bin: str = "/Applications/cmux.app/Contents/Resources/bin/cmux"
    cmux_timeout_seconds: float = 15.0

    # 멀티플렉서 추상화 (DS-70 / MX-20). 현 시점 유효 mux 는 cmux 만.
    # 프로젝트별 transport.mux(agiteam.json)로 override 가능하나, tmux 는 미지원 가드.
    mux: str = "cmux"                  # 기본 멀티플렉서. cross-project 발견은 항상 이 값(cmux) 사용
    tmux_bin: str = "tmux"            # tmux adapter 자리(미구현). 절대경로는 tmux 구현 시 확정

    # 디스커버리/수집 (제우스 2026-06-07 확정)
    # 프로젝트 루트 해소: project_roots(JSON 매핑) 우선, 없으면 projects_base_dir/<project_id>
    projects_base_dir: Path = Field(default_factory=_default_projects_base)
    project_roots_json: str | None = None      # 예: '{"Panthea":"/abs/Panthea"}'
    agiteam_logs_subdir: str = ".agiteam/logs"
    discovery_poll_seconds: float = 5.0
    # 산출물 변경 watcher (DV-70 / DS-100). watchdog 미설치 시 자동 degrade.
    artifact_watcher_enabled: bool = True
    artifact_debounce_seconds: float = 0.3        # DS-60 §11.7 기본 debounce window 300ms
    artifact_hard_flush_seconds: float = 1.0      # burst hard flush 1000ms
    artifact_buffer_ttl_seconds: int = 600        # WG-ART-04 ring buffer TTL 10분
    artifact_buffer_min_keep: int = 1000          # 프로젝트별 최소 보존 건수
    # 에이전트 동작중/조용함 liveness (요구사항 15-1, DS-30 runtime_activity_changed).
    # role.log offset 증가 → active 즉시 전환. 무출력 idle_seconds 경과 → idle 전환(펄럭임 방지).
    # 기본 6초 = rawlog 폴링(logtail_poll_seconds=2s) × 3회.
    activity_idle_seconds: float = 6.0

    # 이미지 첨부 업로드 (DV-90 / DS-120). 저장 root = <project_root>/.agiteam/webgui/uploads/images
    attachment_max_bytes: int = 10 * 1024 * 1024  # 파일당 10 MiB
    attachment_max_per_message: int = 5           # 메시지당 이미지 개수
    attachment_ttl_seconds: int = 24 * 3600       # 임시 업로드 TTL 기본 24h
    attachment_cleanup_seconds: float = 3600.0    # TTL cleanup 주기
    discovery_missed_threshold: int = 2       # cmux tree 누락 N회 초과 시 disconnected
    # transcript 는 hook_stop 트리거가 주 경로(DV-25 정정). 이 폴링은 안전망 fallback 이므로 길게.
    transcript_poll_seconds: float = 30.0     # transcript JSONL canonical tail fallback 주기
    logtail_poll_seconds: float = 2.0         # raw role log 진단 tail 주기
    enable_background: bool = True             # 백그라운드 폴링 루프 on/off (테스트 시 off)

    # 파일/렌더 제한 (DS-20 §13.5 / DS-40 §17.6)
    max_inline_bytes: int = 1_048_576          # 1 MiB: md inline 한계
    max_stream_bytes: int = 52_428_800         # 50 MiB: pdf stream 한계
    max_tree_nodes: int = 2_000                # 트리 1회 응답 노드 상한
    max_tree_depth: int = 6
    render_timeout_seconds: int = 30

    @property
    def auth_required(self) -> bool:
        return self.api_token is not None

    @property
    def collector_auth_required(self) -> bool:
        return self.collector_token is not None

    @property
    def cors_allow_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @property
    def artifacts_root_resolved(self) -> Path:
        return Path(self.artifacts_root).resolve()

    def artifacts_root_for(self, project_id: str | None, root_type: str | None = None) -> Path:
        """project_id·root_type 별 트리 root 해소 (QI-WG-024 정밀화 + 코드탭 확장).

        규약(documents): 모든 프로젝트의 산출물 트리 루트 = `<project_root>/documents`.
        규약(system): 코드(소스) 트리 루트 = `<project_root>/system`.
        root_type 미지정/빈값 = documents (하위호환 필수). 트리 top 노드 이름은 하위
        디렉터리명("documents" 또는 "system")이 된다. UI 드롭다운에서 선택된 project_id 를
        따라 전환한다(AgiTeamApp 특례 없음). projects 와 동일한 project_root() 사용.
        allowlist/traversal 보안은 해소된 root 기준으로 ArtifactService 가 동일 적용한다.
        """
        pid = project_id or self.project_id
        subdir = _root_type_subdir(root_type)
        return (self.project_root(pid) / subdir).resolve()

    def artifacts_display_root_for(self, project_id: str | None, root_type: str | None = None) -> str:
        """응답용 논리 루트 라벨 (host 절대경로 비노출). top 노드는 'documents/' 또는 'system/'."""
        return f"{_root_type_subdir(root_type)}/"

    def project_root(self, project_id: str) -> Path:
        """project_id → 파일시스템 루트. project_roots_json 우선, 없으면 base/<project_id>."""
        import json

        if self.project_roots_json:
            try:
                mapping = json.loads(self.project_roots_json)
                if project_id in mapping:
                    return Path(mapping[project_id]).resolve()
            except (ValueError, TypeError):
                pass
        return (Path(self.projects_base_dir) / project_id).resolve()

    def logs_dir(self, project_id: str) -> Path:
        return self.project_root(project_id) / self.agiteam_logs_subdir

    # --- 실재 프로젝트 식별 (DV-49 / QI-WG-027) ------------------------------
    # 프로젝트명 = 실재 폴더명. cmux workspace_title 은 식별/표시에 쓰지 않는다.

    @staticmethod
    def is_project_dir(root: Path) -> bool:
        """폴더가 실재하는 AgiTeam 프로젝트인지: 디렉터리 존재 + 마커(.agiteam/
        project_state.yaml/agiteam.json) 보유. 이름으로 판단하지 않고 실재만 본다."""
        try:
            if not root.is_dir():
                return False
        except OSError:
            return False
        return (
            (root / ".agiteam").exists()
            or (root / "project_state.yaml").exists()
            or (root / "agiteam.json").exists()
        )

    def project_exists(self, project_id: str) -> bool:
        """project_id 의 root 폴더가 실재 프로젝트인지 (유령/오타 경로 제외)."""
        return self.is_project_dir(self.project_root(project_id))

    def project_display_name(self, project_id: str) -> str:
        """표시명 = 실재 root 폴더명(basename). cmux workspace_title 사용 금지."""
        root = self.project_root(project_id)
        return root.name or project_id


@lru_cache
def get_settings() -> Settings:
    return Settings()
