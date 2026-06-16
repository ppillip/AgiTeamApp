# 골든 픽스처 포맷 정의 (RV-30)

> 대상: AgiTeamApp Python(oracle) ↔ Rust 백엔드 동등성 검증
> SSOT 연계: TS-11 §3(필드3분류)·§5(DB snapshot), TS-10 §3.1(하네스 구조)
> 판정 원칙: 사람이 아니라 도구(골든 픽스처 + canonical-diff)가 판정한다. Python을 oracle로 고정한다.

골든 픽스처는 **3종**이다. 모두 JSON이며 UTF-8.

```
fixtures/<계약ID>/
├── <case>.case.json            # ① 입력셋   — 무엇을 어떤 초기상태로 요청하는가
├── <case>.python.golden.json   # ② 골든출력 — Python oracle 캡처 (정답)
└── <case>.rust.actual.json     # ③ 실측      — Rust 캡처 (검증 대상)
```

DB 덤프는 ②·③ 캡처 안의 `db` 필드로 함께 담는다(별도 파일도 가능하나 케이스 단위 동봉 권장).

---

## ① 입력셋 (`*.case.json`)

| 키 | 필수 | 설명 |
|---|---|---|
| `case_id` | ✅ | 케이스 고유 ID (예: `WG-PROJ-01-happy`) |
| `contract_id` | ✅ | L2 계약 ID (TS-11 §4, 예: `WG-PROJ-01`) |
| `title` | | 사람용 설명 |
| `request` | ✅ | 요청 정의 (아래) |
| `seed` | | 초기상태 참조 (`db_seed`, `mux_mock`, `fs_tree` 등 경로) |
| `array_sort_paths` | | 순서 비결정 배열의 정책경로 목록 (예: `["http.body.data.events[]"]`) |

`request` 객체:

| 키 | 필수 | 설명 |
|---|---|---|
| `kind` | ✅ | `http` 또는 `ws` |
| `method` | http | `GET`/`POST`/... |
| `path` | http | 엔드포인트 경로 |
| `query` | | 쿼리 파라미터 객체 |
| `headers` | | 요청 헤더(토큰 등). 실제 토큰값은 fixture placeholder로 |
| `body` | | 요청 본문(JSON) 또는 multipart 정의 |
| `ws_ops` | ws | WS 시나리오(subscribe/after-cursor/기대 이벤트 트리거) |

## ② 골든출력 / ③ 실측 (`*.python.golden.json` / `*.rust.actual.json`)

두 파일은 **동일 포맷**(`Capture`). `backend`만 다르다.

| 키 | 필수 | 설명 |
|---|---|---|
| `case_id` | ✅ | 입력셋과 동일 ID |
| `backend` | ✅ | `python` 또는 `rust` |
| `http` | | HTTP 응답 캡처: `{status, headers, body, body_b64?}` |
| `ws_events` | | WS envelope 수신순 배열 |
| `db` | | DB 부작용 snapshot: `{<table>: [<row>, ...]}` |
| `mux` | | fake mux capture: `[{argv, cwd, env}, ...]` (RV-20 fake_cmux JSONL 대응) |

- `http.body`: JSON 응답은 파싱된 객체로. 바이너리는 `body_b64`(base64)로.
- `db`: row는 컬럼명→값 객체. JSONB 컬럼은 문자열/객체 모두 허용(도구가 파싱해 canonical 비교).
- `mux`: §6.2 team 커맨드 동등성용. "백엔드가 mux로 무엇을 보내려 했는가"를 비교.

---

## 비교 동작 요약 (도구가 자동 적용)

| 요소 | 처리 |
|---|---|
| object key order | 비교 제외(canonical sort) |
| 서버 생성 UUID | 참조 일관 placeholder(`<UUID#n>`)로 치환 후 비교 → 값이 아니라 참조 구조 비교 |
| timestamp/timestamptz | `<TS#n>` 치환(형식·존재 검증) |
| cursor (`<ts>|kind:<uuid>`) | 부분 치환으로 구조 보존 비교 |
| JSONB 내부 key order | 파싱 후 canonical 비교 |
| host absolute path | `<ABS_PATH>` 치환 + 노출 경고(parity FAIL 아님, RV-50 신호) |
| 순서 비결정 배열 | `array_sort_paths` 지정 시 canonical 정렬 후 비교 |
| 공개금지 키(role_id 등) | rules `forbidden_keys`에 있으면 존재만으로 보안 FAIL |

판정: **strict 정책 필드 diff 0건 + 보안위반 0건 → PASS.** 정규화 후에도 남는 차이는 모두 FAIL.

비교 정책(어느 필드가 strict/normalize/exclude인가)은 `rules/compare-rules.*.json`에 데이터로 분리되어 있으며 **아르고스(QA) 교차검토 대상**이다.
