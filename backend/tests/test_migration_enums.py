"""DS-30 v0.5 최종 enum 잠금 테스트 (QI-WG-006 + 아테나 role_log_collector 확정).

migration check 제약이 확정 enum 과 정합하는지 보장한다.
"""
from __future__ import annotations

from pathlib import Path

_SQL = (Path(__file__).resolve().parents[1] / "migrations" / "0001_init.sql").read_text(encoding="utf-8")


def test_message_source_enum():
    assert (
        "CHECK (source IN ('webgui','pm_bridge','role_log','hook','read_screen'))" in _SQL
    )
    # 폐기 토큰 미존재
    assert "'stdout'" not in _SQL


def test_message_type_enum():
    assert (
        "message_type IN ('user_message','log_line','status','error','unmatched')" in _SQL
    )


def test_runtime_event_source_enum():
    assert (
        "source IN ('cmux_adapter','role_log_collector','hook','read_screen',"
        "'backend','artifact_service','postgres_notify')" in _SQL
    )
    assert "stdout_collector" not in _SQL
