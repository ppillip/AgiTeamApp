"""로그 tail 수집 보조로직 테스트 (제우스 2026-06-07).

DB 비의존 부분(파일명→role, ANSI 제거, offset tail)만 검증한다.
DB 저장 경로(_store)는 PostgreSQL 통합테스트(QA/DevOps PG)에서 검증.
"""
from __future__ import annotations

from app.services.log_collector import (
    LOG_LINE_TYPE,
    ROLE_LOG_SOURCE,
    role_from_filename,
    strip_ansi,
)


def test_canonical_source_and_type():
    # QI-WG-006: 로그 tail 본문의 canonical source/message_type
    assert ROLE_LOG_SOURCE == "role_log"
    assert LOG_LINE_TYPE == "log_line"


def test_role_from_filename():
    assert role_from_filename("PM") == "PM"
    assert role_from_filename("DeveloperBE") == "DeveloperBE"
    assert role_from_filename("be") == "DeveloperBE"
    assert role_from_filename("fe") == "DeveloperFE"
    assert role_from_filename("architect") == "Architect"
    assert role_from_filename("random") is None


def test_strip_ansi():
    raw = "\x1b[32m작업 착수\x1b[0m\x1b[1A했습니다"
    assert strip_ansi(raw) == "작업 착수했습니다"


def test_read_new_tails_only_appended(tmp_path):
    from app.config import get_settings
    from app.services.cmux_discovery import DiscoveryRegistry
    from app.services.log_collector import LogCollector

    get_settings.cache_clear()
    settings = get_settings()
    lc = LogCollector(settings, DiscoveryRegistry(), sessionmaker=None)

    logfile = tmp_path / "PM.log"
    logfile.write_text("기존 히스토리\n", encoding="utf-8")
    # 최초 발견 → EOF 부터 시작(히스토리 재적재 안 함)
    assert lc._read_new(logfile) == ""
    # 새 내용 추가 → 그 부분만 반환
    with open(logfile, "a", encoding="utf-8") as f:
        f.write("새 줄1\n새 줄2\n")
    out = lc._read_new(logfile)
    assert "새 줄1" in out and "새 줄2" in out
    assert "기존 히스토리" not in out
    # 추가 없음 → 빈 문자열
    assert lc._read_new(logfile) == ""


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
    out = lc._read_new(logfile)
    assert "x" in out
