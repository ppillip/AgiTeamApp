"""긴급 결함수정 2026-06-09: /message-updates 폴링 400 무한루프.

원인: message_updates 가 내보내는 next_cursor 는 복합 포맷
``"{recorded_at_iso}|message:{uuid}"`` 인데, after 파라미터 타입이 datetime 이라
FE 가 그 커서를 그대로 되돌려 보내면 datetime 파싱 실패 → 400 무한 반복.
수정: after 를 str 로 받아 _parse_after_cursor 가 복합 커서의 시각부분만 파싱.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.errors import WebguiError
from app.routers.messages import _parse_after_cursor


def test_none_and_empty_return_none():
    assert _parse_after_cursor(None) is None
    assert _parse_after_cursor("") is None


def test_plain_iso_parses():
    dt = _parse_after_cursor("2026-06-08T12:49:37+00:00")
    assert dt == datetime(2026, 6, 8, 12, 49, 37, tzinfo=timezone.utc)


def test_compound_cursor_roundtrip():
    """이 엔드포인트가 내보내는 복합 커서를 그대로 되돌려 받아도 시각으로 파싱돼야 한다."""
    cursor = "2026-06-08T12:49:37+00:00|message:1f3e9c2a-0000-4000-8000-000000000abc"
    dt = _parse_after_cursor(cursor)
    assert dt == datetime(2026, 6, 8, 12, 49, 37, tzinfo=timezone.utc)


def test_compound_cursor_naive_datetime():
    cursor = "2026-06-08T12:49:37|message:abc"
    dt = _parse_after_cursor(cursor)
    assert dt == datetime(2026, 6, 8, 12, 49, 37)


def test_garbage_raises_422():
    with pytest.raises(WebguiError) as ei:
        _parse_after_cursor("not-a-date")
    assert ei.value.http_status == 422
    assert ei.value.code == "invalid_pagination"


def test_garbage_compound_raises_422():
    with pytest.raises(WebguiError) as ei:
        _parse_after_cursor("garbage|message:abc")
    assert ei.value.http_status == 422
