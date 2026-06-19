"""Raw role log 진단 격하 테스트 (DV-25 정정).

raw role log 는 더 이상 말풍선 '본문'(message)으로 저장하지 않고 진단 event 로만 보존한다.
DB 비의존 부분(파일명→role, ANSI 처리, offset tail)만 검증한다.
DB 저장 경로(_store_diagnostic)는 PostgreSQL 통합테스트(QA/DevOps PG)에서 검증.
"""
from __future__ import annotations

from app.services.log_collector import (
    RAW_LOG_SOURCE,
    RAW_TUI_EVENT,
    has_ansi,
    role_from_filename,
    strip_ansi,
)


def test_raw_log_diagnostic_tokens():
    # DV-25: raw role log 는 runtime_event(raw_tui_capture / raw_log_collector)로 격하
    assert RAW_LOG_SOURCE == "raw_log_collector"
    assert RAW_TUI_EVENT == "raw_tui_capture"


def test_role_from_filename():
    assert role_from_filename("PM") == "PM"
    assert role_from_filename("DeveloperBE") == "DeveloperBE"
    assert role_from_filename("be") == "DeveloperBE"
    assert role_from_filename("fe") == "DeveloperFE"
    assert role_from_filename("architect") == "Architect"
    assert role_from_filename("random") is None


def test_strip_ansi_and_has_ansi():
    raw = "\x1b[32m작업 착수\x1b[0m\x1b[1A했습니다"
    assert strip_ansi(raw) == "작업 착수했습니다"
    assert has_ansi(raw) is True
    assert has_ansi("순수 텍스트") is False


def test_read_new_tails_only_appended(tmp_path):
    from app.config import get_settings
    from app.services.cmux_discovery import DiscoveryRegistry
    from app.services.log_collector import LogCollector

    get_settings.cache_clear()
    settings = get_settings()
    lc = LogCollector(settings, DiscoveryRegistry(), sessionmaker=None)

    logfile = tmp_path / "PM.log"
    logfile.write_text("기존 히스토리\n", encoding="utf-8")
    # 최초 발견 → EOF 부터 시작(히스토리 재적재 안 함). (chunk, start) 반환.
    chunk, _ = lc._read_new(logfile)
    assert chunk == ""
    # 새 내용 추가 → 그 부분만 반환
    with open(logfile, "a", encoding="utf-8") as f:
        f.write("새 줄1\n새 줄2\n")
    out, start = lc._read_new(logfile)
    assert "새 줄1" in out and "새 줄2" in out
    assert "기존 히스토리" not in out
    assert start > 0
    # 추가 없음 → 빈 문자열
    assert lc._read_new(logfile)[0] == ""


def test_read_new_handles_truncate(tmp_path):
    from app.config import get_settings
    from app.services.cmux_discovery import DiscoveryRegistry
    from app.services.log_collector import LogCollector

    get_settings.cache_clear()
    lc = LogCollector(get_settings(), DiscoveryRegistry(), sessionmaker=None)
    logfile = tmp_path / "QA.log"
    logfile.write_text("aaaaaaaa\nbbbbbbbb\n", encoding="utf-8")
    lc._read_new(logfile)  # offset = EOF (큰 크기)
    # 로그 회전: 더 작은 크기로 재작성 → size < offset 감지 → 처음부터 다시 읽음
    logfile.write_text("x\n", encoding="utf-8")
    out, _ = lc._read_new(logfile)
    assert "x" in out
