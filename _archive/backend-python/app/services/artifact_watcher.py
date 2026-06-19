"""산출물 변경 watcher (DV-70 / 요구사항 13-3 산출물 폴더 동기화).

설계: DS-100(확정 결정), DS-40 §10.3/§20 (artifact_changed·WG-ART-04), DS-60 §11.7.

구조(§11.7 흐름):
  watchdog raw event(절대경로) → Project Root Resolver(가장 긴 root match)
  → ArtifactService 보안 재사용(traversal/symlink/secret/hidden) → 상대경로+project_id
  → debounce/coalesce(300ms/1000ms, 병합키 project_id+path)
  → artifact_changed publish(기존 message-stream WS hub) + ring buffer(WG-ART-04)

불변 원칙:
- watcher 는 read-only 감시자. 파일을 생성/수정/삭제하지 않는다(§11.7 보안경계).
- 절대경로를 WebSocket/REST/runtime_event payload 에 절대 넣지 않는다. 항상 root 기준 상대경로.
- project_id 를 찾지 못한 이벤트는 버린다(임의 경로 브로드캐스트 금지).
- canonical store 와 무관: 파일 메타/본문은 DB 에 저장하지 않고 메모리 ring buffer 만 둔다.

watchdog 미설치/초기화 실패 시 enabled=False 로 degrade 하며, WG-ART-04 는
`artifact_watcher_unavailable`(503)을 반환한다. FE 는 기존 polling 없이 트리/파일 REST 로만 동작.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from ..config import ROOT_TYPE_SUBDIR, Settings
from ..errors import WebguiError
from .artifact_service import ArtifactService
from .events import hub

logger = logging.getLogger(__name__)

try:  # watchdog 은 선택 의존(미설치 시 degrade) — DS-60 §11.7
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except Exception:  # noqa: BLE001  (ImportError 외 backend 초기화 실패도 degrade)
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment,misc]


# --- 순수 헬퍼 (단위테스트 대상) -------------------------------------------------


def coalesce_kinds(kinds: list[str]) -> str:
    """디바운스 윈도우 내 병합 우선순위 (DS-60 §11.7).

    - `deleted` 가 하나라도 있으면 최종 `deleted`.
    - 첫 이벤트가 `created` 면 `created` (created 뒤 modified 는 created 로 흡수).
    - 그 외는 마지막 이벤트.
    """
    if not kinds:
        return "modified"
    if "deleted" in kinds:
        return "deleted"
    if kinds[0] == "created":
        return "created"
    return kinds[-1]


def normalize_fs_kind(event_type: str) -> str | None:
    """watchdog event_type → API change_type (DS-40 §10.3).

    moved 는 호출부에서 src(deleted)/dest(created) 2건으로 분해하므로 여기서는 다루지 않는다.
    """
    if event_type in ("created", "closed_no_write"):
        return "created"
    if event_type in ("modified", "closed"):
        return "modified"
    if event_type == "deleted":
        return "deleted"
    return None


def _iso_z(ts: datetime) -> str:
    """ISO-8601 UTC, 밀리초 + Z (DS-40 예시 `2026-06-11T03:40:12.345Z`)."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _ms_floor(ts: datetime) -> datetime:
    """datetime 을 밀리초 단위로 절단(UTC). cursor(_iso_z, ms) 와 정밀도를 맞춰 비교 정확성 확보."""
    u = ts.astimezone(timezone.utc)
    return u.replace(microsecond=(u.microsecond // 1000) * 1000)


def _compact_ts(ts: datetime) -> str:
    """update_id 용 compact UTC (`20260611T034012345Z`)."""
    u = ts.astimezone(timezone.utc)
    return u.strftime("%Y%m%dT%H%M%S") + f"{u.microsecond // 1000:03d}Z"


def make_cursor(ts: datetime, rel_path: str, root_type: str | None = None) -> str:
    """`timestamp|artifact:[<root_type>:]<urlencoded relative_path>` (DS-40 §20 / DS-60 §11.7).

    root_type(system|persona) 가 주어지면 cursor suffix 에 포함해 같은 상대경로가 다른 root_type
    에서 충돌하지 않게 한다(코드/페르소나 탭, 2026-06-14). documents/None 은 하위호환 위해 생략.
    timestamp 부분은 불변이라 parse_cursor_ts 와 호환된다.
    """
    prefix = f"{root_type}:" if root_type and root_type != "documents" else ""
    return f"{_iso_z(ts)}|artifact:{prefix}{quote(rel_path, safe='')}"


def make_update_id(ts: datetime, rel_path: str, root_type: str | None = None) -> str:
    """`artifact:<compact_ts>:[<root_type>:]<relative_path>` (DS-40 §10.3 예시).

    root_type 포함 시 dedup 키 충돌 방지(다른 root_type 의 동일 경로·동일 ms). documents/None 은 생략.
    """
    prefix = f"{root_type}:" if root_type and root_type != "documents" else ""
    return f"artifact:{_compact_ts(ts)}:{prefix}{rel_path}"


def parent_path(rel_path: str) -> str:
    """부모 디렉토리 상대경로. root 직하는 빈 문자열 (DS-40 §10.3)."""
    if "/" not in rel_path:
        return ""
    return rel_path.rsplit("/", 1)[0]


def parse_cursor_ts(after: str) -> datetime:
    """WG-ART-04 `after` cursor 에서 timestamp 부분 파싱.

    포맷 `timestamp|artifact:<path>`. 순수 ISO 시각도 허용한다. 실패 시 ValueError.
    """
    ts_part = after.split("|artifact:", 1)[0] if "|artifact:" in after else after
    ts_part = ts_part.strip()
    if ts_part.endswith("Z"):
        ts_part = ts_part[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_part)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_cursor_key(after: str) -> tuple[datetime, str | None, str | None]:
    """WG-ART-04 cursor → 복합 정렬키 (ts, root_type, rel_path).

    make_cursor 포맷: `{iso}|artifact:[{root_type}:]{urlencoded rel_path}` (documents 는 prefix 생략).
    같은 millisecond 의 다중 root_type 이벤트를 정확히 페이지네이션하기 위해 root_type+path 까지 복원한다.

    - rel_path 가 None 이면 timestamp-only cursor(하위호환) → 호출부에서 'ts 초과'로 처리.
    - root_type prefix 가 없으면 documents 로 복원(make_cursor 가 documents 일 때 생략하므로).
    - quote(safe='') 인코딩이라 rel_path 안에는 ':' 가 존재하지 않는다 → 첫 ':' 로 안전 분리.
    실패 시 ValueError(상위에서 CursorParseError 로 변환).
    """
    ts = parse_cursor_ts(after)
    art = after.partition("|artifact:")[2] if "|artifact:" in after else ""
    if art == "":
        return ts, None, None       # timestamp-only
    root_type = "documents"
    rest = art
    if ":" in art:
        head, _, tail = art.partition(":")
        if head in ROOT_TYPE_SUBDIR:   # documents|system|persona
            root_type = head
            rest = tail
    return ts, root_type, unquote(rest)


def node_type_of(abs_path: str, final_kind: str, is_directory_hint: bool) -> str:
    """변경 path 의 node_type 판정 (DS-40 §10.3: file/directory/unknown).

    deleted 후 stat 불가 시 unknown 허용 (DS-60 §11.7).
    """
    p = Path(abs_path)
    try:
        if p.is_dir():
            return "directory"
        if p.exists():
            return "file"
    except OSError:
        pass
    if final_kind == "deleted":
        # 삭제로 더 이상 stat 불가. hint 가 있으면 활용, 없으면 unknown.
        return "directory" if is_directory_hint else ("unknown")
    # created/modified 인데 stat 실패 → hint 로 보정
    return "directory" if is_directory_hint else "file"


# --- WG-ART-04 ring buffer -------------------------------------------------------


class CursorExpired(Exception):
    """after cursor 가 buffer/TTL 밖 (DS-40 §20: artifact_change_cursor_expired)."""


class CursorParseError(Exception):
    """after cursor 형식 오류 (DS-40 §20: invalid_pagination)."""


class _BufferEntry:
    __slots__ = ("data", "ts", "cursor")

    def __init__(self, data: dict[str, Any], ts: datetime, cursor: str) -> None:
        self.data = data
        self.ts = ts
        self.cursor = cursor


class ArtifactChangeBuffer:
    """프로젝트별 최근 산출물 변경 ring buffer (WG-ART-04, DS-60 §11.7).

    보존 = 프로젝트별 최소 `min_keep` 건 또는 `ttl_seconds` TTL. process memory MVP.
    """

    def __init__(self, ttl_seconds: int = 600, min_keep: int = 1000) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._min_keep = min_keep
        self._by_project: dict[str, deque[_BufferEntry]] = {}

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def append(self, project_id: str, data: dict[str, Any], ts: datetime, cursor: str) -> None:
        dq = self._by_project.setdefault(project_id, deque())
        dq.append(_BufferEntry(data, ts, cursor))
        self._prune(dq)

    def _prune(self, dq: deque[_BufferEntry]) -> None:
        cutoff = self._now() - self._ttl
        # 최소 min_keep 건은 TTL 무관 보존, 그 이상에서 오래된 것부터 제거
        while len(dq) > self._min_keep and dq[0].ts < cutoff:
            dq.popleft()

    @staticmethod
    def _entry_key(e: "_BufferEntry") -> tuple[datetime, str, str]:
        """복합 정렬키 (ms절단 ts, root_type, rel_path). 같은 ms 다중 root_type 을 전순서로 구분.

        ⚠️ cursor 의 ts 는 make_cursor/_iso_z 로 '밀리초까지만' 표현된다. entry.ts 는
        coalescer flush 시 root_type 별로 따로 now() 를 찍어 마이크로초가 제각각이다. 그대로
        비교하면 cursor(ms절단)가 entry(us)보다 작아 같은 ms 이벤트를 재포함한다(아르고스 결함).
        → key 의 ts 를 ms 로 절단해 cursor 와 정밀도를 맞추고, 동률은 root_type·path 로 가른다.
        """
        return (_ms_floor(e.ts), (e.data.get("root_type") or "documents"), (e.data.get("path") or ""))

    def changes_after(
        self, project_id: str, after: str | None, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        dq = self._by_project.get(project_id) or deque()
        # 복합키로 안정 정렬 — append 순서가 같은 ms 내에서 복합키 순서와 달라도 경계를 정확히.
        ordered = sorted(dq, key=self._entry_key)
        if after is None or after == "":
            items = ordered[-limit:]
            return [e.data for e in items], (items[-1].cursor if items else None)
        try:
            after_ts, after_rt, after_path = parse_cursor_key(after)
        except (ValueError, TypeError) as exc:
            raise CursorParseError(str(exc)) from exc
        # cursor 가 가장 오래된 보존분보다 앞서면 그 사이 이벤트가 prune 되었을 수 있다 → 만료.
        # 비교도 ms 절단 기준(cursor 정밀도)으로 맞춰 같은 ms 경계에서 오탐 만료를 막는다.
        if ordered and after_ts < _ms_floor(ordered[0].ts):
            raise CursorExpired()
        if after_path is None:
            # 하위호환: timestamp-only cursor → 기존 동작(그 ms 초과)
            out = [e for e in ordered if _ms_floor(e.ts) > after_ts][:limit]
        else:
            # 복합키 비교: 이미 소비한 이벤트(키 <= cursor)는 제외 → 같은 ms 중복/누락 없음.
            after_key = (after_ts, after_rt, after_path)
            out = [e for e in ordered if self._entry_key(e) > after_key][:limit]
        return [e.data for e in out], (out[-1].cursor if out else None)


# --- 디바운스/코얼레싱 ----------------------------------------------------------


class _Bucket:
    __slots__ = (
        "project_id",
        "root_type",
        "rel_path",
        "abs_path",
        "is_directory",
        "kinds",
        "count",
        "debounce_handle",
        "hard_handle",
    )

    def __init__(self, project_id: str, root_type: str, rel_path: str) -> None:
        self.project_id = project_id
        self.root_type = root_type
        self.rel_path = rel_path
        self.abs_path = ""
        self.is_directory = False
        self.kinds: list[str] = []
        self.count = 0
        self.debounce_handle: asyncio.TimerHandle | None = None
        self.hard_handle: asyncio.TimerHandle | None = None


class Coalescer:
    """병합키 project_id+path 로 raw FS 이벤트를 묶어 path 별 1개 artifact_changed 로 flush.

    - debounce window: 동일 key 신규 이벤트마다 타이머 reset (기본 300ms).
    - burst hard flush: 최초 이벤트 후 일정시간(기본 1000ms) 내 반드시 1회 flush.
    - flush 시 sink(change) 호출. sink 는 publish + ring buffer 적재 책임을 가진다.

    반드시 event loop 스레드에서만 add/flush 가 호출되어야 한다(watchdog 스레드는
    call_soon_threadsafe 로 진입). 타이머는 loop.call_later 를 사용한다.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        sink: Callable[[dict[str, Any], datetime, str], None],
        *,
        debounce_seconds: float = 0.3,
        hard_flush_seconds: float = 1.0,
    ) -> None:
        self._loop = loop
        self._sink = sink
        self._debounce = debounce_seconds
        self._hard = hard_flush_seconds
        self._pending: dict[tuple[str, str, str], _Bucket] = {}

    def add(
        self,
        project_id: str,
        rel_path: str,
        abs_path: str,
        kind: str,
        is_directory: bool,
        root_type: str = "documents",
    ) -> None:
        # 병합키에 root_type 포함 — 같은 상대경로가 documents/system/persona 에서 충돌 안 나게.
        key = (project_id, root_type, rel_path)
        bucket = self._pending.get(key)
        if bucket is None:
            bucket = _Bucket(project_id, root_type, rel_path)
            self._pending[key] = bucket
            bucket.hard_handle = self._loop.call_later(self._hard, self._flush, key)
        bucket.abs_path = abs_path
        bucket.is_directory = is_directory
        bucket.kinds.append(kind)
        bucket.count += 1
        if bucket.debounce_handle is not None:
            bucket.debounce_handle.cancel()
        bucket.debounce_handle = self._loop.call_later(self._debounce, self._flush, key)

    def _flush(self, key: tuple[str, str, str]) -> None:
        bucket = self._pending.pop(key, None)
        if bucket is None:
            return
        if bucket.debounce_handle is not None:
            bucket.debounce_handle.cancel()
        if bucket.hard_handle is not None:
            bucket.hard_handle.cancel()

        final_kind = coalesce_kinds(bucket.kinds)
        ts = datetime.now(timezone.utc)
        ntype = node_type_of(bucket.abs_path, final_kind, bucket.is_directory)
        rel = bucket.rel_path
        rt = bucket.root_type
        data = {
            "update_id": make_update_id(ts, rel, rt),
            "project_id": bucket.project_id,
            "root_type": rt,                     # FE 계약(2026-06-14): documents|system|persona
            "change_type": final_kind,
            "path": rel,                         # 상대경로 = 해당 root_type 루트 기준
            "node_type": ntype,
            "parent_path": parent_path(rel),
            "timestamp": _iso_z(ts),
            "event_count": bucket.count,
            "coalesced": bucket.count > 1,
        }
        cursor = make_cursor(ts, rel, rt)
        try:
            self._sink(data, ts, cursor)
        except Exception:  # noqa: BLE001  (sink 실패가 watcher 를 죽이지 않음)
            logger.exception("artifact change sink failed key=%s", key)

    def flush_all(self) -> None:
        """대기 중 버킷을 즉시 모두 flush (종료 정리용)."""
        for key in list(self._pending.keys()):
            self._flush(key)


# --- watcher 본체 ---------------------------------------------------------------


class _Handler(FileSystemEventHandler):  # type: ignore[misc,valid-type]
    """watchdog 콜백(observer 스레드) → loop 로 안전 전달."""

    def __init__(self, watcher: "ArtifactWatcher") -> None:
        self._watcher = watcher

    def on_any_event(self, event: "FileSystemEvent") -> None:  # type: ignore[override]
        et = getattr(event, "event_type", None)
        is_dir = bool(getattr(event, "is_directory", False))
        if et == "moved":
            src = getattr(event, "src_path", None)
            dest = getattr(event, "dest_path", None)
            if src:
                self._watcher._submit(str(src), "deleted", is_dir)
            if dest:
                self._watcher._submit(str(dest), "created", is_dir)
            return
        kind = normalize_fs_kind(et or "")
        if kind is None:
            return
        src = getattr(event, "src_path", None)
        if src:
            self._watcher._submit(str(src), kind, is_dir)


class WatchTarget:
    __slots__ = ("project_id", "root_type", "root", "service")

    def __init__(self, project_id: str, root_type: str, root: Path, service: ArtifactService) -> None:
        self.project_id = project_id
        self.root_type = root_type      # documents | system | persona
        self.root = root
        self.service = service


class ArtifactWatcher:
    """프로젝트별 documents·system·brain(persona) 3루트 감시 + artifact_changed 브로드캐스트.

    각 이벤트는 root_type(documents|system|persona)을 포함하고, 상대경로는 해당 root 기준이다.

    호출 순서:
      w = ArtifactWatcher(settings, registry)
      w.start()   # lifespan startup (running loop 안에서 호출)
      ...
      await w.stop()
    """

    def __init__(
        self,
        settings: Settings,
        registry: Any | None = None,
        *,
        publish: Callable[[str, dict[str, Any], str], Any] | None = None,
        buffer: ArtifactChangeBuffer | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        # publish 주입(테스트용). 기본은 기존 message-stream WS hub.publish.
        self._publish = publish or hub.publish
        self.buffer = buffer or ArtifactChangeBuffer(
            ttl_seconds=getattr(settings, "artifact_buffer_ttl_seconds", 600),
            min_keep=getattr(settings, "artifact_buffer_min_keep", 1000),
        )
        self.enabled = False
        self._observer: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._coalescer: Coalescer | None = None
        self._targets: list[WatchTarget] = []

    # --- 감시 대상 해소 (§11.7 라우팅 규칙) ---

    def resolve_targets(self) -> list[WatchTarget]:
        """감시할 (project_id, root_type, <root>) 목록 해소.

        원천: project_roots_json 키 ∪ 디스커버리 registry 알려진 프로젝트 ∪ settings.project_id.
        각 실재 프로젝트마다 ROOT_TYPE_SUBDIR(documents·system·brain[persona]) 3루트를 모두
        후보로 잡고, 실제 디렉토리가 존재하는 root 만 채택한다(코드/페르소나 탭, 2026-06-14).
        루트 하드코딩 금지 — config 매핑 재사용.
        """
        s = self._settings
        pids: set[str] = set()
        if s.project_roots_json:
            import json

            try:
                pids.update(json.loads(s.project_roots_json).keys())
            except (ValueError, TypeError):
                pass
        if self._registry is not None:
            try:
                for p in self._registry.projects():
                    pid = p.get("project_id")
                    if pid:
                        pids.add(pid)
            except Exception:  # noqa: BLE001
                pass
        if s.project_id:
            pids.add(s.project_id)

        targets: list[WatchTarget] = []
        for pid in sorted(pids):
            try:
                if not s.project_exists(pid):
                    continue
            except Exception:  # noqa: BLE001
                continue
            for root_type in ROOT_TYPE_SUBDIR:        # documents, system, persona
                try:
                    root = s.artifacts_root_for(pid, root_type)
                    if not root.is_dir():
                        continue
                    display = s.artifacts_display_root_for(pid, root_type)
                    targets.append(
                        WatchTarget(pid, root_type, root, ArtifactService(root, display_root=display))
                    )
                except Exception:  # noqa: BLE001
                    continue
        return targets

    def _match_target(self, abs_path: str) -> tuple[WatchTarget, str] | None:
        """절대경로 → (target, 상대경로). 여러 root 중첩 시 가장 긴 match (§11.7)."""
        try:
            p = Path(abs_path).resolve()
        except (OSError, RuntimeError):
            p = Path(abs_path)
        best: tuple[WatchTarget, str] | None = None
        best_len = -1
        for t in self._targets:
            try:
                rel = p.relative_to(t.root)
            except ValueError:
                continue
            root_len = len(str(t.root))
            if root_len > best_len:
                best_len = root_len
                best = (t, rel.as_posix())
        return best

    @staticmethod
    def _passes_security(service: ArtifactService, rel: str) -> bool:
        """ArtifactService 보안 재사용 + dotfile/hidden 세그먼트 차단 (§11.7 제외목록)."""
        if rel == "" or rel == ".":
            return False
        for seg in rel.split("/"):
            if not seg or seg.startswith("."):  # .git, .DS_Store, hidden/secret 후보
                return False
        try:
            service.resolve(rel)  # traversal/절대/symlink-escape/secret/hidden 재사용
        except WebguiError:
            return False
        return True

    # --- watchdog 스레드 → loop 브릿지 ---

    def _submit(self, abs_path: str, kind: str, is_directory: bool) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._on_raw_event, abs_path, kind, is_directory)

    def _on_raw_event(self, abs_path: str, kind: str, is_directory: bool) -> None:
        """loop 스레드에서 실행: root match → 보안검증 → coalescer 적재."""
        matched = self._match_target(abs_path)
        if matched is None:
            return  # project_id 미해소 → drop (임의 경로 브로드캐스트 금지)
        target, rel = matched
        if not self._passes_security(target.service, rel):
            return
        if self._coalescer is not None:
            self._coalescer.add(
                target.project_id, rel, abs_path, kind, is_directory, root_type=target.root_type
            )

    def _emit(self, data: dict[str, Any], ts: datetime, cursor: str) -> None:
        """flush sink: ring buffer 적재 + WS 브로드캐스트 (기존 message-stream 재사용)."""
        project_id = data["project_id"]
        self.buffer.append(project_id, data, ts, cursor)
        payload = {"type": "artifact_changed", "cursor": cursor, "data": data}
        # 별도 WS 채널 신설 금지(DS-40 §10.4). sentinel room + project_id 격리.
        # message-stream 의 project-wide 구독자(rooms=None)가 자기 프로젝트 변경만 받는다.
        room_id = f"__artifacts__:{project_id}"
        if self._loop is not None:
            self._loop.create_task(self._publish(room_id, payload, project_id))

    # --- lifecycle ---

    def start(self) -> None:
        if not WATCHDOG_AVAILABLE:
            logger.warning("watchdog 미설치 — artifact watcher 비활성(WG-ART-04 degrade)")
            self.enabled = False
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("실행 중 event loop 없음 — artifact watcher 비활성")
            self.enabled = False
            return

        self._targets = self.resolve_targets()
        if not self._targets:
            logger.warning("감시 대상 프로젝트 없음 — artifact watcher 비활성")
            self.enabled = False
            return

        self._coalescer = Coalescer(
            self._loop,
            self._emit,
            debounce_seconds=getattr(self._settings, "artifact_debounce_seconds", 0.3),
            hard_flush_seconds=getattr(self._settings, "artifact_hard_flush_seconds", 1.0),
        )
        try:
            self._observer = Observer()
            handler = _Handler(self)
            for t in self._targets:
                self._observer.schedule(handler, str(t.root), recursive=True)
            self._observer.start()
        except Exception:  # noqa: BLE001
            logger.exception("watchdog observer 시작 실패 — artifact watcher 비활성")
            self.enabled = False
            self._observer = None
            return

        self.enabled = True
        logger.info(
            "artifact watcher 시작: %d개 루트 감시 %s",
            len(self._targets),
            [f"{t.project_id}:{t.root_type}" for t in self._targets],
        )

    async def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:  # noqa: BLE001
                logger.exception("watchdog observer 정지 실패")
            self._observer = None
        if self._coalescer is not None:
            self._coalescer.flush_all()
        self.enabled = False
