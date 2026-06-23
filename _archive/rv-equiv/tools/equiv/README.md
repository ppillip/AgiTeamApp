# tools/equiv — Python↔Rust 백엔드 동등성 canonical-diff 도구 (RV-30)

AgiTeamApp 백엔드 `backend/`(Python·oracle) ↔ `backend-rs/`(Rust) 동등성 검증의
**공용 비교 엔진**이다. RV-30 산출물.

> ⚠️ 본 도구는 `backend/`·`backend-rs/` 제품 코드를 **수정하지 않는다**(읽기 전용 대상).
> ⚠️ **판정 주체는 사람이 아니라 이 도구**다. Python을 oracle로 고정하고, 골든 픽스처와
> canonical-diff로 기계 판정한다. 비교 정책은 코드가 아니라 데이터(rules JSON)로 분리되어
> 아르고스(QA) 교차검토를 받는다.

## 무엇을 하나

같은 입력에 대한 Python 응답(골든)과 Rust 응답(실측)을, RV-10 비교정책
(strict/normalize/exclude)에 따라 **필드별로** 비교한다.

- 바이트 비교 금지 → 파싱 후 의미 비교(canonical JSON)
- UUID·timestamp·cursor·JSONB 키순서·host path 등 비결정 요소를 정규화/마스킹
- WS·poller 시간의존 배열은 정렬키 sort 후 비교
- 공개금지 키(role_id 등) 노출은 보안 FAIL

## 구조

```
tools/equiv/
├── parity/                  # 엔진 (표준 라이브러리만, 외부 의존 0)
│   ├── canonical.py         # canonical JSON (key order 제거)
│   ├── normalizer.py        # 비결정 요소 정규화 (참조 일관 placeholder)
│   ├── rules.py             # 비교정책 로더 (TS-11 §3 기계화)
│   ├── compare.py           # canonical-diff 엔진
│   ├── fixtures.py          # 골든 픽스처 로더/검증
│   └── cli.py               # CLI (compare / normalize)
├── rules/
│   └── compare-rules.sample.json   # 비교정책 샘플 (아르고스 교차검토 대상)
├── fixtures/<계약ID>/        # 골든 픽스처 샘플 (META-01, WG-PROJ-01)
├── schemas/golden-fixture.md # 픽스처 3종 포맷 정의
└── tests/test_parity.py      # 도구 자체 단위테스트 (21건)
```

## 사용법

```bash
cd system/AgiTeamApp/tools/equiv

# 골든(Python) vs 실측(Rust) 비교 → PASS는 exit 0, FAIL은 exit 1 (CI 연동)
python3 -m parity.cli compare \
  --rules  rules/compare-rules.sample.json \
  --golden fixtures/WG-PROJ-01/projects.python.golden.json \
  --actual fixtures/WG-PROJ-01/projects.rust.actual.json \
  --case   fixtures/WG-PROJ-01/projects.case.json

# 단일 문서 정규화 결과 확인(정책 디버깅)
python3 -m parity.cli normalize --file fixtures/WG-PROJ-01/projects.rust.actual.json

# 도구 자체 검증
python3 -m unittest discover -s tests -v
```

## 라이브러리로 사용 (RV-40 runner가 import)

```python
from parity import compare_documents, load_rules
from parity.fixtures import load_capture

rules = load_rules("rules/compare-rules.sample.json")
py   = load_capture("...python.golden.json").comparable()
rust = load_capture("...rust.actual.json").comparable()
result = compare_documents(py, rust, rules, contract_id="WG-PROJ-01")
assert result.passed, result.summary()
```

## 판정 기준

`result.passed` == (strict diff 0건) AND (normalize 후 잔여 diff 0건) AND (보안위반 0건).
host absolute path 노출은 `warnings`로만 보고하며 parity FAIL이 아니다(RV-50 보안리뷰 신호).

## 후속 RV 연계

| WBS | 본 도구 사용 |
|---|---|
| RV-40 | 테스트케이스/데이터가 `*.case.json`을 생성, runner가 `parity.compare_documents` 호출 |
| RV-50 | 정적 인스펙션이 `warnings`(path 노출)·forbidden_keys 결과 활용 |
| RV-60 | E2E 실행이 골든 캡처 후 본 도구로 계약별 PASS/FAIL 산출 |
| RV-70 | 결함대장이 `diffs[].kind`(value_mismatch/missing/type/array_length)로 분류 |

## 범위 (RV-30)

도구 + 픽스처 포맷 + 샘플 2계약(META-01, WG-PROJ-01) 동작 검증까지. 전체 테스트케이스
투입(RV-40)·E2E 전수 실행(RV-60)은 후속 트랙이다.
