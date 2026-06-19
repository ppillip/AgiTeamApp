"""에이전트 동작중/조용함 liveness 단위테스트 (요구사항 15-1).

순수 판정(decide_activity)·offset→활동 통합(_update_activity)을 DB 없이 검증한다.
DB 저장/WS publish 경로(_emit_activity_change)는 PostgreSQL 통합테스트(QA/DevOps)에서 검증.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.log_collector import (
    ACTIVITY_ACTIVE,
    ACTIVITY_IDLE,
    ACTIVITY_UNKNOWN,
    REASON_OUTPUT,
    REASON_QUIET,
    RUNTIME_ACTIVITY_EVENT,
    ActivityRegistry,
    ActivityState,
    LogCollector,
    decide_activity,
)


_BASE = datetime(2026, 6, 12, 0, 0, 0, tzinfo=timezone.utc)


def _t(sec: int) -> datetime:
    return _BASE + timedelta(seconds=sec)


# --- 순수 판정 규칙 ---------------------------------------------------------

def test_unknown_to_active_on_first_output():
    st = ActivityState()
    d = decide_activity(st, had_output=True, now=_t(0), idle_threshold_seconds=6.0)
    assert d is not None
    activity, reason, idle_for = d
    assert activity == ACTIVITY_ACTIVE
    assert reason == REASON_OUTPUT
    assert idle_for == 0.0  # 최초 출력은 직전 active 시각 없음 → 0


def test_active_stays_active_no_transition_on_more_output():
    st = ActivityState(activity=ACTIVITY_ACTIVE, last_active_ts=_t(0))
    # 이미 active 인데 또 출력 → 전환 없음(이벤트 없음)
    assert decide_activity(st, had_output=True, now=_t(2), idle_threshold_seconds=6.0) is None


def test_active_to_idle_only_after_threshold():
    st = ActivityState(activity=ACTIVITY_ACTIVE, last_active_ts=_t(0))
    # 4초 무출력 (threshold 6초 미만) → 아직 active 유지
    assert decide_activity(st, had_output=False, now=_t(4), idle_threshold_seconds=6.0) is None
    # 6초 경과 → idle 전환
    d = decide_activity(st, had_output=False, now=_t(6), idle_threshold_seconds=6.0)
    assert d is not None
    activity, reason, idle_for = d
    assert activity == ACTIVITY_IDLE
    assert reason == REASON_QUIET
    assert idle_for == pytest.approx(6.0)


def test_idle_back_to_active_immediately():
    st = ActivityState(activity=ACTIVITY_IDLE, last_active_ts=_t(0))
    d = decide_activity(st, had_output=True, now=_t(10), idle_threshold_seconds=6.0)
    assert d is not None
    activity, reason, idle_for = d
    assert activity == ACTIVITY_ACTIVE
    assert reason == REASON_OUTPUT
    assert idle_for == pytest.approx(10.0)  # 조용했던 시간 참고값


def test_idle_no_repeat_transition():
    st = ActivityState(activity=ACTIVITY_IDLE, last_active_ts=_t(0))
    # 이미 idle 인데 계속 무출력 → 재전환 없음(중복 이벤트 금지)
    assert decide_activity(st, had_output=False, now=_t(100), idle_threshold_seconds=6.0) is None


# --- offset → 활동 통합 (_update_activity, DB 비의존) ------------------------

class _StubRegistry:
    def projects(self):
        return []

    def resolve(self, project_id, role):
        return None


@pytest.fixture
def collector(tmp_path):
    from app.config import get_settings

    get_settings.cache_clear()
    act = ActivityRegistry()
    lc = LogCollector(get_settings(), _StubRegistry(), sessionmaker=None, activity=act)
    return lc, act


@pytest.mark.asyncio
async def test_update_activity_flow(collector, tmp_path, monkeypatch):
    lc, act = collector
    path = tmp_path / "PM.log"

    # _emit_activity_change 를 가로채 전환 payload 만 포착(DB/WS 우회)
    emitted = []

    async def _fake_emit(project_id, role, payload, now):
        emitted.append(payload)
        return 1

    monkeypatch.setattr(lc, "_emit_activity_change", _fake_emit)

    # 1) 출력 있음 → active 전환
    n = await lc._update_activity("P", "PM", path, "안녕\n", 0, _t(0))
    assert n == 1
    assert act.get("P", "PM") == ACTIVITY_ACTIVE
    assert emitted[-1]["runtime_activity"] == ACTIVITY_ACTIVE
    assert emitted[-1]["reason"] == REASON_OUTPUT
    assert emitted[-1]["from_activity"] == ACTIVITY_UNKNOWN
    assert emitted[-1]["chunk_bytes"] > 0
    assert emitted[-1]["offset_start"] == 0
    assert emitted[-1]["idle_threshold_seconds"] == lc.settings.activity_idle_seconds

    # 2) 추가 출력 → active 유지, 전환 이벤트 없음
    n = await lc._update_activity("P", "PM", path, "또 출력\n", 7, _t(2))
    assert n == 0
    assert act.get("P", "PM") == ACTIVITY_ACTIVE

    # 3) 무출력 4초 (threshold 미만) → 전환 없음
    n = await lc._update_activity("P", "PM", path, "", 0, _t(6))
    assert n == 0
    assert act.get("P", "PM") == ACTIVITY_ACTIVE

    # 4) 무출력 누적 6초 경과 → idle 전환 (last_active_ts=_t(2) 기준)
    n = await lc._update_activity("P", "PM", path, "", 0, _t(8))
    assert n == 1
    assert act.get("P", "PM") == ACTIVITY_IDLE
    assert emitted[-1]["runtime_activity"] == ACTIVITY_IDLE
    assert emitted[-1]["reason"] == REASON_QUIET
    assert emitted[-1]["from_activity"] == ACTIVITY_ACTIVE
    assert emitted[-1]["chunk_bytes"] == 0
    assert emitted[-1]["idle_for_seconds"] == pytest.approx(6.0)

    # 5) 다시 출력 → 즉시 active 복귀
    n = await lc._update_activity("P", "PM", path, "재개\n", 14, _t(20))
    assert n == 1
    assert act.get("P", "PM") == ACTIVITY_ACTIVE
    assert emitted[-1]["from_activity"] == ACTIVITY_IDLE


def test_event_token_constants():
    assert RUNTIME_ACTIVITY_EVENT == "runtime_activity_changed"
