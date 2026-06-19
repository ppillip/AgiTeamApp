"""correlation 매칭 순수 로직 (DS-20 §11.6, DS-60 §6.5)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class OutboundCandidate:
    correlation_id: str
    status: str
    occurred_at: datetime
    closed: bool = False


def pick_correlation(candidates: list[OutboundCandidate]) -> str | None:
    """열린(sent, not-closed) outbound 중 가장 최근 correlation_id 선택.

    매칭 후보가 없으면 None (=> 호출자가 unmatched 로 저장).
    """
    open_sent = [c for c in candidates if c.status == "sent" and not c.closed and c.correlation_id]
    if not open_sent:
        return None
    open_sent.sort(key=lambda c: c.occurred_at, reverse=True)
    return open_sent[0].correlation_id
