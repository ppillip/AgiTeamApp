"""AgiTeamApp Python↔Rust 백엔드 동등성 검증 공용 도구 (RV-30).

이 패키지는 backend/·backend-rs/ 제품 코드를 수정하지 않는다(읽기 전용 대상).
판정 주체는 사람이 아니라 본 도구(골든 픽스처 + canonical-diff)다.

구성:
- canonical : object key order를 제거한 canonical JSON 변환/직렬화
- normalizer: 비결정 요소(UUID/timestamp/cursor/JSONB key order/local path) 정규화
- rules     : 비교정책(strict/normalize/exclude) 로더 — RV-10 TS-11 §3 기계화
- compare   : canonical-diff 엔진 — 정책을 필드별로 적용해 diff 산출
- fixtures  : 골든 픽스처(입력셋/골든출력/DB덤프) 포맷 로더·검증
"""

from .compare import ParityResult, Diff, compare_documents
from .normalizer import normalize, NormalizeStats
from .rules import CompareRules, Policy, load_rules
from .canonical import canonicalize, canonical_dumps

__all__ = [
    "ParityResult",
    "Diff",
    "compare_documents",
    "normalize",
    "NormalizeStats",
    "CompareRules",
    "Policy",
    "load_rules",
    "canonicalize",
    "canonical_dumps",
]
