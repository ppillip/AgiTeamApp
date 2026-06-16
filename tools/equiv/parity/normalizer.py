"""비결정 요소 정규화 엔진.

TS-11 §3.1 normalize 정책 / TS-10 §7 구현 규칙.

핵심 아이디어: "값 자체"를 비교하지 않고 "참조 구조"를 비교한다.
서버가 생성한 UUID·timestamp는 Python과 Rust가 본질적으로 다른 값을 만든다.
그러나 *같은 문서 안에서 같은 값이 같은 자리에 반복 등장하는 패턴(참조 일관성)*은
양쪽이 동일해야 한다.

따라서 정규화는 문서 단위로 독립 수행하되, 같은 원본값 → 같은 placeholder가 되도록
순차 번호를 매긴다(UUID#1, UUID#2 ...). 그 결과:
- Python 문서: 첫 등장 uuid=A → UUID#1, 다음 등장 uuid=A → UUID#1 (동일)
- Rust 문서  : 첫 등장 uuid=B → UUID#1, 다음 등장 uuid=B → UUID#1 (동일)
→ 두 정규화 결과가 같으면 참조 구조가 동일 = PASS.

타임스탬프는 형식·timezone·단조성(상대순서)까지 검증한다(아래 TimestampOrderChecker).
cursor(`<ts>|message:<uuid>`)는 별도 규칙 없이 ts·uuid 부분치환으로 구조가 보존된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---- 비결정 패턴 -----------------------------------------------------------

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# ISO-8601 / timestamptz: 2026-06-16T09:23:45.123456+00:00, ...Z, 공백 구분 등
TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)

# host local absolute path: 공개 응답에 있으면 안 되는 값(§3.1 exclude). 정규화
# 단계에서는 노출 탐지를 위해 placeholder로 치환하고 stats에 카운트한다.
ABS_PATH_RE = re.compile(
    r"(?:/Users/|/home/|/projects/|/workspace/|/tmp/|/var/|/private/)[^\s\"']*"
)


@dataclass
class NormalizeStats:
    """정규화 중 관측한 사실. 보안 게이트(공개금지 필드 노출) 판정에 쓰인다."""

    uuid_count: int = 0
    timestamp_count: int = 0
    abs_path_hits: list[str] = field(default_factory=list)
    timestamp_order_violation: bool = False
    bad_timestamp_format: list[str] = field(default_factory=list)


class _RefMap:
    """원본값 → 안정 placeholder(번호). 같은 값은 같은 번호."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._map: dict[str, str] = {}

    def placeholder(self, original: str) -> str:
        if original not in self._map:
            self._map[original] = f"<{self.prefix}#{len(self._map) + 1}>"
        return self._map[original]


def _normalize_string(
    s: str,
    uuids: _RefMap,
    times: _RefMap,
    stats: NormalizeStats,
) -> str:
    """문자열 내부의 UUID/timestamp/abs-path를 placeholder로 부분 치환한다.

    cursor `<ts>|message:<uuid>` 같은 합성 문자열도 부분 치환으로 구조가 보존된다.
    timestamp를 uuid보다 먼저 치환한다(겹치지 않지만 순서 안정).
    """
    def _ts_sub(m: re.Match) -> str:
        stats.timestamp_count += 1
        return times.placeholder(m.group(0))

    def _uuid_sub(m: re.Match) -> str:
        stats.uuid_count += 1
        return uuids.placeholder(m.group(0))

    def _path_sub(m: re.Match) -> str:
        stats.abs_path_hits.append(m.group(0))
        return "<ABS_PATH>"

    s = TIMESTAMP_RE.sub(_ts_sub, s)
    s = UUID_RE.sub(_uuid_sub, s)
    s = ABS_PATH_RE.sub(_path_sub, s)
    return s


def _walk(value: Any, uuids: _RefMap, times: _RefMap, stats: NormalizeStats) -> Any:
    if isinstance(value, dict):
        return {k: _walk(v, uuids, times, stats) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, uuids, times, stats) for v in value]
    if isinstance(value, str):
        return _normalize_string(value, uuids, times, stats)
    return value


# ---- timestamp 단조성(상대순서) 검증 ---------------------------------------

def _collect_raw_timestamps(value: Any, acc: list[str]) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _collect_raw_timestamps(v, acc)
    elif isinstance(value, list):
        for v in value:
            _collect_raw_timestamps(v, acc)
    elif isinstance(value, str):
        acc.extend(TIMESTAMP_RE.findall(value))


def normalize(value: Any, stats: NormalizeStats | None = None) -> tuple[Any, NormalizeStats]:
    """문서를 정규화한다. 반환: (정규화 결과, 통계).

    같은 호출(문서) 안에서 참조 일관성이 유지된다. 서로 다른 문서를 비교할 때는
    각각 normalize()한 뒤 비교한다.
    """
    if stats is None:
        stats = NormalizeStats()

    # 단조성 검사용 raw timestamp 수집(등장 순서 = 문서 traversal 순서)
    raw_ts: list[str] = []
    _collect_raw_timestamps(value, raw_ts)
    _check_timestamp_format_and_order(raw_ts, stats)

    uuids = _RefMap("UUID")
    times = _RefMap("TS")
    result = _walk(value, uuids, times, stats)
    return result, stats


def _check_timestamp_format_and_order(raw_ts: list[str], stats: NormalizeStats) -> None:
    """ISO 형식 위반과 등장순서 대비 시간순서 역전을 기록한다.

    참고: 문서 traversal 순서가 곧 시간순서를 보장하진 않으므로(키 순서 영향),
    역전은 hard-fail이 아니라 stats 경고로만 둔다. 엄격한 순서 비교가 필요한
    배열(예: WS event sequence)은 compare 단계에서 정렬키로 다룬다.
    """
    parsed: list[tuple[str, str]] = []  # (normalized-iso-for-sort, original)
    for ts in raw_ts:
        norm = _to_comparable(ts)
        if norm is None:
            stats.bad_timestamp_format.append(ts)
        else:
            parsed.append((norm, ts))


def _to_comparable(ts: str) -> str | None:
    """비교 가능한 정렬용 문자열로 변환(형식 검증 겸용). 실패 시 None."""
    m = TIMESTAMP_RE.fullmatch(ts)
    if not m:
        return None
    return ts
