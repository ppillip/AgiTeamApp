"""correlation 매칭 순수 로직 테스트 (DS-60 §6.5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.correlation import OutboundCandidate, pick_correlation

T0 = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_pick_most_recent_open_sent():
    cands = [
        OutboundCandidate("c1", "sent", T0),
        OutboundCandidate("c2", "sent", T0 + timedelta(seconds=10)),
    ]
    assert pick_correlation(cands) == "c2"


def test_skip_closed_and_failed():
    cands = [
        OutboundCandidate("c1", "sent", T0 + timedelta(seconds=30), closed=True),
        OutboundCandidate("c2", "failed", T0 + timedelta(seconds=20)),
        OutboundCandidate("c3", "sent", T0 + timedelta(seconds=5)),
    ]
    assert pick_correlation(cands) == "c3"


def test_no_open_returns_none():
    assert pick_correlation([OutboundCandidate("c1", "failed", T0)]) is None
    assert pick_correlation([]) is None
