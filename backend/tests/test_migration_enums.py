"""DS-30 v0.7 / DV-25 enum 잠금 테스트 (수집 방향 확정: hook/transcript canonical).

migration check 제약이 확정 enum 과 정합하는지 보장한다.
- message source: webgui/pm_bridge(호환) + bridge/hook/transcript(canonical). role_log/read_screen 본문 source 폐기.
- message_type: user_message/assistant_message/status/error/unmatched. log_line 폐기.
- runtime_event source: conversation_collector/transcript_parser 추가, raw_log_collector 확정.
"""
from __future__ import annotations

from pathlib import Path

_SQL = (Path(__file__).resolve().parents[1] / "migrations" / "0001_init.sql").read_text(encoding="utf-8")


def test_message_source_enum():
    assert (
        "CHECK (source IN ('webgui','pm_bridge','bridge','hook','transcript'))" in _SQL
    )
    # 폐기 토큰 미존재
    assert "'stdout'" not in _SQL
    assert "'role_log'" not in _SQL  # raw role log 는 message source 가 아니라 runtime_event


def test_message_type_enum():
    assert (
        "message_type IN ('user_message','assistant_message','status','error','unmatched')" in _SQL
    )
    assert "'log_line'" not in _SQL  # tee raw stdout 본문 파서 폐기


def test_runtime_event_source_enum():
    assert (
        "source IN ('cmux_adapter','conversation_collector','transcript_parser',"
        "'raw_log_collector','hook','read_screen','backend','artifact_service','postgres_notify')"
        in _SQL
    )
    assert "stdout_collector" not in _SQL
    assert "role_log_collector" not in _SQL  # raw_log_collector 로 확정


def test_transcript_columns_and_dedupe():
    # DS-30 §4.3 transcript 추적 컬럼
    assert "transcript_record_id text" in _SQL
    assert "provider             text" in _SQL
    # DS-30 §5 provider+record dedupe unique index
    assert "idx_webgui_message_provider_record" in _SQL
