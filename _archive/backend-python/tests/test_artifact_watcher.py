"""산출물 변경 watcher 단위테스트 (DV-70 / DS-100, DS-40 §10.3·§20, DS-60 §11.7).

검증 범위(PM 지정):
- 파일 생성/수정/삭제 → artifact_changed 이벤트 발행
- 디바운스/코얼레싱 병합 우선순위
- WG-ART-04 ring buffer cursor (정상/만료/형식오류)
- 보안 재사용(traversal/secret/symlink/hidden) + project_id 라우팅
- 실 watchdog observer end-to-end 배선
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.services import artifact_watcher as aw
from app.services.artifact_watcher import (
    ArtifactChangeBuffer,
    ArtifactWatcher,
    Coalescer,
    CursorExpired,
    CursorParseError,
    coalesce_kinds,
    make_cursor,
    node_type_of,
    normalize_fs_kind,
    parent_path,
    parse_cursor_key,
    parse_cursor_ts,
)
from app.services.artifact_watcher import _iso_z


# --- 순수 헬퍼 ------------------------------------------------------------------


def test_coalesce_kinds_priority():
    # deleted 가 있으면 최종 deleted
    assert coalesce_kinds(["created", "modified", "deleted"]) == "deleted"
    assert coalesce_kinds(["modified", "deleted", "created"]) == "deleted"
    # created 뒤 modified 는 created 로 흡수
    assert coalesce_kinds(["created", "modified", "modified"]) == "created"
    assert coalesce_kinds(["created"]) == "created"
    # 그 외는 마지막 이벤트
    assert coalesce_kinds(["modified", "modified"]) == "modified"
    assert coalesce_kinds([]) == "modified"


def test_normalize_fs_kind():
    assert normalize_fs_kind("created") == "created"
    assert normalize_fs_kind("modified") == "modified"
    assert normalize_fs_kind("deleted") == "deleted"
    assert normalize_fs_kind("closed") == "modified"
    assert normalize_fs_kind("opened") is None


def test_parent_path():
    assert parent_path("04.development/02.설계/DS-40/a.md") == "04.development/02.설계/DS-40"
    assert parent_path("top.md") == ""
    assert parent_path("a/b") == "a"


def test_parse_cursor_ts_roundtrip_and_z():
    ts = datetime(2026, 6, 11, 3, 40, 12, 345000, tzinfo=timezone.utc)
    cur = make_cursor(ts, "04.development/02.설계/a.md")
    parsed = parse_cursor_ts(cur)
    assert parsed == ts
    # 순수 ISO + Z 도 허용
    assert parse_cursor_ts("2026-06-11T03:40:12.345Z") == ts
    with pytest.raises(ValueError):
        parse_cursor_ts("not-a-timestamp")


def test_make_cursor_urlencodes_path():
    ts = datetime(2026, 6, 11, 3, 40, 12, 345000, tzinfo=timezone.utc)
    cur = make_cursor(ts, "04.development/02.설계/a b.md")
    # 슬래시·공백·한글이 인코딩되어 cursor 파싱 안전
    assert "|artifact:" in cur
    assert " " not in cur.split("|artifact:")[1]


def test_node_type_of(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hi", encoding="utf-8")
    d = tmp_path / "sub"
    d.mkdir()
    assert node_type_of(str(f), "modified", False) == "file"
    assert node_type_of(str(d), "created", True) == "directory"
    # 삭제되어 stat 불가 + hint 없음 → unknown
    assert node_type_of(str(tmp_path / "gone.md"), "deleted", False) == "unknown"


# --- WG-ART-04 ring buffer ------------------------------------------------------


def _entry(buf: ArtifactChangeBuffer, pid: str, path: str, ts: datetime):
    buf.append(pid, {"path": path, "project_id": pid}, ts, make_cursor(ts, path))


def test_buffer_changes_after_returns_new_only():
    buf = ArtifactChangeBuffer(ttl_seconds=600, min_keep=1000)
    base = datetime(2026, 6, 11, 3, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _entry(buf, "Panthea", f"a{i}.md", base + timedelta(seconds=i))
    # 처음(after=None)엔 전체
    updates, cursor = buf.changes_after("Panthea", None, 100)
    assert len(updates) == 5
    # 2초 시점 이후만
    after = make_cursor(base + timedelta(seconds=2), "a2.md")
    updates2, _ = buf.changes_after("Panthea", after, 100)
    assert [u["path"] for u in updates2] == ["a3.md", "a4.md"]


def test_buffer_cursor_expired_when_before_oldest():
    buf = ArtifactChangeBuffer(ttl_seconds=600, min_keep=2)
    now = datetime.now(timezone.utc)
    # min_keep=2 이므로 오래된 것 prune 가능하게 충분히 추가(TTL 밖)
    old = now - timedelta(hours=2)
    for i in range(5):
        _entry(buf, "P", f"f{i}.md", old + timedelta(seconds=i))
    # 가장 오래된 보존분보다 앞선 cursor → 만료
    far_before = make_cursor(old - timedelta(hours=1), "x.md")
    with pytest.raises(CursorExpired):
        buf.changes_after("P", far_before, 100)


def test_buffer_invalid_cursor():
    buf = ArtifactChangeBuffer()
    _entry(buf, "P", "a.md", datetime.now(timezone.utc))
    with pytest.raises(CursorParseError):
        buf.changes_after("P", "garbage|artifact:zzz", 100)


def test_buffer_project_isolation():
    buf = ArtifactChangeBuffer()
    now = datetime.now(timezone.utc)
    _entry(buf, "ProjA", "a.md", now)
    _entry(buf, "ProjB", "b.md", now)
    updates, _ = buf.changes_after("ProjA", None, 100)
    assert [u["path"] for u in updates] == ["a.md"]


# --- 복합키 페이지네이션: 같은 ms 다중 root_type 중복 결함 (아르고스 발견, 2026-06-14) ----

def _entry_rt(buf, pid, path, ts, root_type):
    buf.append(pid, {"path": path, "project_id": pid, "root_type": root_type},
               ts, make_cursor(ts, path, root_type))


def test_parse_cursor_key_roundtrip():
    ts = datetime(2026, 6, 14, 13, 0, 0, 123000, tzinfo=timezone.utc)
    # system/persona: prefix 포함, 복원 정확
    for rt in ("system", "persona"):
        cur = make_cursor(ts, "PM/persona.md", rt)
        pts, prt, ppath = parse_cursor_key(cur)
        assert prt == rt and ppath == "PM/persona.md"
    # documents: prefix 생략 → documents 로 복원
    cur_d = make_cursor(ts, "a b.md", "documents")
    _pts, prt_d, ppath_d = parse_cursor_key(cur_d)
    assert prt_d == "documents" and ppath_d == "a b.md"   # urldecode 까지 정확
    # timestamp-only(하위호환): artifact part 없음 → path None
    _t, rt_n, path_n = parse_cursor_key(_iso_z(ts))
    assert rt_n is None and path_n is None


def test_buffer_no_dup_same_ms_multiroot():
    """같은 상대경로를 3루트에 동시(같은 ms) 생성 → 3건 매칭 → 재조회 시 0건(중복 없음)."""
    buf = ArtifactChangeBuffer()
    ts = datetime(2026, 6, 14, 13, 0, 0, tzinfo=timezone.utc)
    # 핵심 재현: coalescer 처럼 root_type 별 ts 가 마이크로초만 다르고 cursor 는 ms 절단된다.
    for i, rt in enumerate(("system", "documents", "persona")):   # append 순서 ≠ 정렬 순서(의도)
        _entry_rt(buf, "P", "same.txt", ts + timedelta(microseconds=i * 11), rt)
    updates, cursor = buf.changes_after("P", None, 100)
    assert len(updates) == 3
    assert sorted(u["root_type"] for u in updates) == ["documents", "persona", "system"]
    # next_cursor 재조회 → 0건
    again, _ = buf.changes_after("P", cursor, 100)
    assert again == []
    # 이후 새 변경만 잡힘
    ts2 = ts + timedelta(milliseconds=5)
    _entry_rt(buf, "P", "new.txt", ts2, "system")
    after_new, _ = buf.changes_after("P", cursor, 100)
    assert [u["path"] for u in after_new] == ["new.txt"]


def test_buffer_limit_boundary_same_ms():
    """같은 ms 3건을 limit=2 로 '전진' 분할 조회 → 복합키 정렬·경계 정확(누락/중복 0).

    forward pagination 의미: 시작 cursor(그룹 직전)부터 newer 방향으로 limit 씩.
    """
    buf = ArtifactChangeBuffer()
    base = datetime(2026, 6, 14, 13, 0, 0, tzinfo=timezone.utc)
    _entry_rt(buf, "P", "old.md", base, "documents")
    ts = base + timedelta(milliseconds=10)
    # entry.ts 에 마이크로초 편차를 일부러 줘서 ms절단 효과를 검증(append 순서도 섞음)
    for i, rt in enumerate(("system", "documents", "persona")):
        _entry_rt(buf, "P", "x.txt", ts + timedelta(microseconds=i * 7), rt)
    start = make_cursor(base, "old.md", "documents")        # 그룹 직전 cursor
    page1, c1 = buf.changes_after("P", start, 2)
    assert [u["root_type"] for u in page1] == ["documents", "persona"]   # 복합키 정렬
    page2, c2 = buf.changes_after("P", c1, 2)
    assert [u["root_type"] for u in page2] == ["system"]                 # 나머지 1건(중복 없음)
    page3, _ = buf.changes_after("P", c2, 2)
    assert page3 == []                                                   # 더 없음


def test_buffer_timestamp_only_cursor_backcompat():
    """순수 ISO timestamp-only cursor → 기존 동작(ts 초과) 유지."""
    buf = ArtifactChangeBuffer()
    base = datetime(2026, 6, 14, 13, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        _entry_rt(buf, "P", f"a{i}.md", base + timedelta(seconds=i), "documents")
    updates, _ = buf.changes_after("P", _iso_z(base), 100)
    assert [u["path"] for u in updates] == ["a1.md", "a2.md"]


# --- Coalescer (디바운스/병합) — create/modify/delete → 이벤트 발행 ----------------


@pytest.mark.asyncio
async def test_coalescer_emits_created_then_modified_as_created():
    loop = asyncio.get_running_loop()
    emitted: list[dict] = []
    coal = Coalescer(
        loop,
        lambda data, ts, cursor: emitted.append(data),
        debounce_seconds=0.05,
        hard_flush_seconds=0.5,
    )
    # 같은 path 에 created + modified burst
    coal.add("Panthea", "04.development/a.md", "/abs/04.development/a.md", "created", False)
    coal.add("Panthea", "04.development/a.md", "/abs/04.development/a.md", "modified", False)
    await asyncio.sleep(0.15)
    assert len(emitted) == 1
    d = emitted[0]
    assert d["change_type"] == "created"        # 병합 우선순위
    assert d["path"] == "04.development/a.md"
    assert d["project_id"] == "Panthea"
    assert d["event_count"] == 2
    assert d["coalesced"] is True
    assert d["parent_path"] == "04.development"


@pytest.mark.asyncio
async def test_coalescer_delete_wins():
    loop = asyncio.get_running_loop()
    emitted: list[dict] = []
    coal = Coalescer(loop, lambda d, t, c: emitted.append(d), debounce_seconds=0.05, hard_flush_seconds=0.5)
    coal.add("P", "x.md", "/abs/x.md", "modified", False)
    coal.add("P", "x.md", "/abs/x.md", "deleted", False)
    await asyncio.sleep(0.15)
    assert len(emitted) == 1
    assert emitted[0]["change_type"] == "deleted"


@pytest.mark.asyncio
async def test_coalescer_separate_paths_emit_separately():
    loop = asyncio.get_running_loop()
    emitted: list[dict] = []
    coal = Coalescer(loop, lambda d, t, c: emitted.append(d), debounce_seconds=0.05, hard_flush_seconds=0.5)
    coal.add("P", "a.md", "/abs/a.md", "created", False)
    coal.add("P", "b.md", "/abs/b.md", "modified", False)
    await asyncio.sleep(0.15)
    paths = sorted(d["path"] for d in emitted)
    assert paths == ["a.md", "b.md"]
    # 단일 이벤트는 coalesced=False
    assert all(d["event_count"] == 1 and d["coalesced"] is False for d in emitted)


@pytest.mark.asyncio
async def test_coalescer_hard_flush_under_continuous_burst():
    loop = asyncio.get_running_loop()
    emitted: list[dict] = []
    coal = Coalescer(loop, lambda d, t, c: emitted.append(d), debounce_seconds=0.2, hard_flush_seconds=0.3)
    # debounce(0.2)보다 짧은 간격으로 계속 add → debounce 만으론 flush 안 됨. hard flush(0.3) 가 강제.
    for _ in range(8):
        coal.add("P", "busy.md", "/abs/busy.md", "modified", False)
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)
    assert len(emitted) >= 1  # hard flush 로 최소 1회 발행
    assert emitted[0]["path"] == "busy.md"


# --- 보안 재사용 + project_id 라우팅 (ArtifactWatcher) ---------------------------


def _make_settings(monkeypatch, proj_root: Path, pid: str) -> Settings:
    monkeypatch.setenv("WEBGUI_PROJECT_ID", pid)
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps({pid: str(proj_root)}))
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    return Settings()


def _watcher_with_target(monkeypatch, tmp_path: Path):
    proj = tmp_path / "MyProj"
    docs = proj / "documents"
    (docs / "04.development" / "02.설계").mkdir(parents=True)
    (proj / ".agiteam").mkdir()  # project_exists 마커
    settings = _make_settings(monkeypatch, proj, "MyProj")
    w = ArtifactWatcher(settings, registry=None)
    w._targets = w.resolve_targets()
    return w, docs


def test_resolve_targets_and_match(monkeypatch, tmp_path):
    w, docs = _watcher_with_target(monkeypatch, tmp_path)
    assert len(w._targets) == 1
    assert w._targets[0].project_id == "MyProj"
    abs_path = str(docs / "04.development" / "02.설계" / "a.md")
    matched = w._match_target(abs_path)
    assert matched is not None
    target, rel = matched
    assert target.project_id == "MyProj"
    assert rel == "04.development/02.설계/a.md"


def test_match_target_drops_outside_root(monkeypatch, tmp_path):
    w, _docs = _watcher_with_target(monkeypatch, tmp_path)
    assert w._match_target("/etc/passwd") is None


def test_security_blocks_secret_and_hidden(monkeypatch, tmp_path):
    w, _docs = _watcher_with_target(monkeypatch, tmp_path)
    svc = w._targets[0].service
    # 정상 경로 통과
    assert w._passes_security(svc, "04.development/02.설계/a.md") is True
    # secret/hidden/traversal 차단 (ArtifactService 재사용 + dotfile 세그먼트)
    assert w._passes_security(svc, ".env") is False
    assert w._passes_security(svc, "secret.key") is False
    assert w._passes_security(svc, ".git/config") is False
    assert w._passes_security(svc, "04.development/.DS_Store") is False
    assert w._passes_security(svc, "../escape.md") is False
    assert w._passes_security(svc, "") is False


@pytest.mark.asyncio
async def test_on_raw_event_routes_to_coalescer(monkeypatch, tmp_path):
    w, docs = _watcher_with_target(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    w._loop = loop
    emitted: list[dict] = []
    w._coalescer = Coalescer(loop, w._emit, debounce_seconds=0.05, hard_flush_seconds=0.5)
    # _emit 가 publish 호출 — publish 를 가로채 검증
    captured: list[tuple] = []

    async def fake_publish(room_id, payload, project_id):
        captured.append((room_id, payload, project_id))

    w._publish = fake_publish
    abs_path = str(docs / "04.development" / "a.md")
    Path(abs_path).write_text("x", encoding="utf-8")
    w._on_raw_event(abs_path, "created", False)
    await asyncio.sleep(0.15)
    assert len(captured) == 1
    room_id, payload, project_id = captured[0]
    assert project_id == "MyProj"
    assert room_id == "__artifacts__:MyProj"          # sentinel room (별도 채널 신설 안 함)
    assert payload["type"] == "artifact_changed"
    assert payload["data"]["path"] == "04.development/a.md"
    assert payload["data"]["change_type"] == "created"
    # ring buffer 에도 적재되어 WG-ART-04 로 조회 가능
    updates, _ = w.buffer.changes_after("MyProj", None, 100)
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_on_raw_event_drops_secret(monkeypatch, tmp_path):
    w, docs = _watcher_with_target(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    w._loop = loop
    captured: list = []
    w._publish = lambda *a: captured.append(a) or _noop()
    w._coalescer = Coalescer(loop, w._emit, debounce_seconds=0.05, hard_flush_seconds=0.5)
    w._on_raw_event(str(docs / ".env"), "modified", False)
    await asyncio.sleep(0.12)
    assert captured == []  # secret 은 발행 안 됨


async def _noop():
    return None


# --- 실 watchdog observer end-to-end 배선 ----------------------------------------


@pytest.mark.asyncio
async def test_watchdog_e2e_file_lifecycle(monkeypatch, tmp_path):
    """실제 watchdog observer 로 파일 생성/수정/삭제 → artifact_changed 발행 검증."""
    proj = tmp_path / "E2EProj"
    docs = proj / "documents" / "04.development"
    docs.mkdir(parents=True)
    (proj / ".agiteam").mkdir()
    settings = _make_settings(monkeypatch, proj, "E2EProj")
    settings.artifact_debounce_seconds = 0.1
    settings.artifact_hard_flush_seconds = 0.4

    w = ArtifactWatcher(settings, registry=None)
    captured: list[dict] = []

    async def fake_publish(room_id, payload, project_id):
        captured.append(payload["data"])

    w._publish = fake_publish
    w.start()
    assert w.enabled is True
    try:
        target = docs / "spec.md"
        # 생성
        target.write_text("# v1", encoding="utf-8")
        await _wait_for(lambda: any(d["change_type"] == "created" for d in captured), 3.0)
        # 수정
        target.write_text("# v2 changed", encoding="utf-8")
        await _wait_for(
            lambda: any(d["change_type"] == "modified" and d["path"].endswith("spec.md") for d in captured)
            or any(d["change_type"] == "created" and d["event_count"] > 1 for d in captured),
            3.0,
        )
        # 삭제
        target.unlink()
        await _wait_for(lambda: any(d["change_type"] == "deleted" for d in captured), 3.0)
    finally:
        await w.stop()
        assert w.enabled is False

    paths = {d["path"] for d in captured}
    assert any(p.endswith("spec.md") for p in paths)
    # 모든 발행 path 는 상대경로(절대경로 비노출)
    assert all(not d["path"].startswith("/") for d in captured)
    assert all(d["project_id"] == "E2EProj" for d in captured)


async def _wait_for(cond, timeout: float):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within timeout")


# --- 멀티루트 + root_type 이벤트 (코드/페르소나 탭, 2026-06-14) -------------------


def _watcher_multiroot(monkeypatch, tmp_path: Path):
    """documents·system·brain 3루트를 모두 scaffold 한 watcher."""
    proj = tmp_path / "MultiProj"
    (proj / "documents" / "04.development").mkdir(parents=True)
    (proj / "system" / "backend").mkdir(parents=True)
    (proj / "brain" / "PM").mkdir(parents=True)
    (proj / ".agiteam").mkdir()
    settings = _make_settings(monkeypatch, proj, "MultiProj")
    w = ArtifactWatcher(settings, registry=None)
    w._targets = w.resolve_targets()
    return w, proj


def test_resolve_targets_multiroot(monkeypatch, tmp_path):
    w, _proj = _watcher_multiroot(monkeypatch, tmp_path)
    by_rt = {t.root_type: t for t in w._targets}
    assert set(by_rt) == {"documents", "system", "persona"}    # 3루트 모두 schedule 대상
    assert by_rt["persona"].root.name == "brain"               # persona → brain
    assert by_rt["system"].root.name == "system"
    assert all(t.project_id == "MultiProj" for t in w._targets)


def test_resolve_targets_skips_missing_root(monkeypatch, tmp_path):
    """system/brain 디렉토리가 없는 프로젝트는 documents 만 잡힌다(존재하는 root 만)."""
    proj = tmp_path / "DocsOnly"
    (proj / "documents").mkdir(parents=True)
    (proj / ".agiteam").mkdir()
    settings = _make_settings(monkeypatch, proj, "DocsOnly")
    w = ArtifactWatcher(settings, registry=None)
    targets = w.resolve_targets()
    assert {t.root_type for t in targets} == {"documents"}


def test_match_target_resolves_root_type(monkeypatch, tmp_path):
    w, proj = _watcher_multiroot(monkeypatch, tmp_path)
    # system 루트 하위 파일 → root_type=system, 상대경로는 system 기준
    m_sys = w._match_target(str(proj / "system" / "backend" / "app.py"))
    assert m_sys is not None and m_sys[0].root_type == "system"
    assert m_sys[1] == "backend/app.py"
    # brain 루트 하위 → root_type=persona
    m_persona = w._match_target(str(proj / "brain" / "PM" / "persona.md"))
    assert m_persona is not None and m_persona[0].root_type == "persona"
    assert m_persona[1] == "PM/persona.md"


@pytest.mark.asyncio
async def test_coalesce_key_includes_root_type():
    """같은 상대경로라도 root_type 이 다르면 별도 버킷으로 flush(충돌 방지)."""
    loop = asyncio.get_running_loop()
    flushed: list[dict] = []
    coal = Coalescer(loop, lambda data, ts, cur: flushed.append(data),
                     debounce_seconds=0.05, hard_flush_seconds=0.5)
    coal.add("P", "PM/persona.md", "/abs/brain/PM/persona.md", "modified", False, root_type="persona")
    coal.add("P", "PM/persona.md", "/abs/system/PM/persona.md", "modified", False, root_type="system")
    await asyncio.sleep(0.15)
    rts = sorted(d["root_type"] for d in flushed)
    assert rts == ["persona", "system"]              # 2건(병합 안 됨)
    assert {d["path"] for d in flushed} == {"PM/persona.md"}


@pytest.mark.asyncio
async def test_on_raw_event_emits_root_type(monkeypatch, tmp_path):
    """system 파일 변경 → payload·cursor 에 root_type=system 포함."""
    w, proj = _watcher_multiroot(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    w._loop = loop
    w._coalescer = Coalescer(loop, w._emit, debounce_seconds=0.05, hard_flush_seconds=0.5)
    captured: list[tuple] = []

    async def fake_publish(room_id, payload, project_id):
        captured.append((room_id, payload, project_id))

    w._publish = fake_publish
    abs_path = str(proj / "system" / "backend" / "app.py")
    Path(abs_path).write_text("x=1", encoding="utf-8")
    w._on_raw_event(abs_path, "created", False)
    await asyncio.sleep(0.15)
    assert len(captured) == 1
    _room, payload, _pid = captured[0]
    data = payload["data"]
    assert data["root_type"] == "system"             # FE 계약
    assert data["path"] == "backend/app.py"          # system 루트 기준 상대경로
    assert "system:" in payload["cursor"]            # cursor 에 root_type 반영
    # ring buffer 조회 시에도 root_type 포함
    updates, _ = w.buffer.changes_after("MultiProj", None, 100)
    assert updates and updates[-1]["root_type"] == "system"


def test_watcher_degrades_when_no_targets(monkeypatch, tmp_path):
    # 실재하지 않는 프로젝트만 설정 → 감시 대상 0 → enabled False
    monkeypatch.setenv("WEBGUI_PROJECT_ID", "Ghost")
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps({"Ghost": str(tmp_path / "nope")}))
    settings = Settings()
    w = ArtifactWatcher(settings, registry=None)

    async def _run():
        w.start()

    asyncio.run(_run())
    assert w.enabled is False
