# AgiTeamApp WebGUI — Backend (DV-20)

모니터(AgiTeamApp) WebGUI 백엔드. FastAPI 기반.

설계 산출물(소스 of truth):
- `documents/products/AgiTeamApp/04.development/02.설계/DS-20_아키텍처설계서`
- `.../DS-30_DB설계서`
- `.../DS-40_인터페이스명세서/DS-40_API명세서.md`
- `.../DS-60_연동규격서`

## 확정 스택 (DS-20 §15)

| 영역 | 스택 |
|---|---|
| 언어/프레임워크 | Python + FastAPI |
| DB | PostgreSQL (uuid/timestamptz/jsonb/partial·GIN index) |
| 통신 | REST + WebSocket update channel + polling fallback |
| 수집 | role 로그파일 tail(`.agiteam/logs/<role>.log`, source=role_log/message_type=log_line) + read-screen 보강 + hook normalizer + PM Bridge 선저장 |

## 구현 범위 (이번 인도분)

| WBS | 내용 | 엔드포인트 |
|---|---|---|
| DV-20.1 | 메시지 채널 + PM 브릿지 (PM 송신·응답 수신) | WG-MSG-01/02/03/04, WG-MSG-05(WS), WG-CHAT-05/06 |
| DV-20.2 | 팀원별 채팅 저장·조회 (관찰 뷰) | WG-CHAT-01/02/03/04 |
| DV-20.3 | 디렉토리 트리 읽기 | WG-ART-01 |
| DV-20.4 | 파일 내용 읽기 (형식 판별) | WG-ART-02/03 |

### 라우팅 정책 (제우스 확정 2026-06-07)

- 오케스트레이터 = 웹사용자(휴먼).
- **WebGUI 사용자의 모든 발신은 PM(제우스) surface 로만 전달된다.**
  `POST /api/webgui/messages` 의 cmux 송신 대상은 항상 PM 고정. 팀원 surface 직접 송신 경로는 없다.
- 팀원별 채팅방(WG-CHAT)은 PM↔팀원 대화를 role/surface 별로 분리 **조회만** 하는 읽기 뷰.
  대화 본문은 로그 tail collector 가 채운다.

### 2026-06-07 보강 (디스커버리·수집·surface 비의존·멀티프로젝트)

1. **cmux 디스커버리**: `cmux tree --all` 파싱 → workspace=project_id, surface title `"이름(역할)"`
   에서 role 추출 → (project_id, role)→surface 매핑을 백엔드가 자체 구성(`cmux_discovery.py`).
   AgiTeam(agiteam.sh) 은 건드리지 않는다.
2. **liveness**: `cmux tree` 주기 폴링(기본 5s). 매핑에 있던 surface 가 tree 에서 사라지면
   `connection_state=disconnected`. 송신 직전 `read-screen` 핑으로 확정.
3. **대화 수집(변경)**: 화면긁기/stdout 직접수집 폐기 → `<project_root>/.agiteam/logs/<role>.log`
   tail 로 수집(`log_collector.py`, 기본 2s) → DB 방별 분리 저장.
4. **surface 비의존**: room/message 식별·저장 키 = **(project_id, role)**. `surface_id` 는 송신 직전
   레지스트리에서 동적 해소하는 일시값. surface 가 바뀌어도 식별/저장 안 깨짐.
5. **cmux 절대경로**: `/Applications/cmux.app/Contents/Resources/bin/cmux` (PATH 비의존, 설정값).
6. **멀티프로젝트**: `GET /api/webgui/projects`(DB 불요). rooms/runtime/messages 는 `project_id` 스코프.

## 디렉터리

```
backend/
  app/
    main.py            # FastAPI 앱 팩토리, 오류 핸들러, 라우터 include
    config.py          # 환경설정 (WEBGUI_ 접두)
    deps.py            # 인증·DB 세션 의존성
    errors.py          # DS-40 오류 envelope/코드
    schemas/           # Pydantic 요청·응답 모델 (DS-40 §4)
    db/
      base.py          # async 엔진/세션 (lazy)
      models.py        # DS-30 4테이블 ORM
      repositories.py  # 조회·저장 쿼리
      serializers.py   # ORM -> 응답 dict
    services/
      pm_bridge.py     # PM 고정 송신 (선저장->cmux->상태갱신)
      cmux_adapter.py  # cmux send + send-key Enter (arg 배열, atomic)
      collector_service.py  # inbound 수집·correlation·dedupe
      correlation.py   # correlation 매칭 순수로직
      artifact_service.py   # allowlist+traversal 차단, 트리/파일/형식판별/sanitize
      masking.py       # secret 마스킹
      events.py        # in-process WebSocket hub
    routers/           # runtime/messages/rooms/collector/artifacts
  migrations/0001_init.sql   # PostgreSQL DDL (DS-30 §11)
  tests/               # 단위·스모크 테스트
```

## 설치 / 실행

```bash
cd system/AgiTeamApp/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # 필요 시 값 수정

# (DB 사용 시) PostgreSQL 준비 후 스키마 적용
psql "$WEBGUI_DATABASE_URL" -f migrations/0001_init.sql

# 서버 기동
PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8731
# 문서: http://127.0.0.1:8731/docs
```

## 테스트

```bash
PYTHONPATH=. pytest        # 45 passed
```

- 보안(traversal/절대경로/symlink/숨김·secret/형식판별)·마스킹·cmux·correlation 단위테스트는 **DB 불요**.
- 산출물 트리/파일 엔드포인트 스모크 테스트도 DB 불요.
- 메시지/채팅(DB 의존) 엔드포인트의 전체 런타임 검증에는 PostgreSQL 이 필요하다(아래 참고).

## 보안 (DS-20 §13, DS-40 §21, TS-70)

- 산출물 접근은 `documents/products/AgiTeamApp/` allowlist 루트로 제한.
- traversal(`..`)·절대경로·드라이브/UNC·symlink escape·null byte·숨김/secret 파일 차단.
- `system/AgiTeamBuilder/` 등 루트 밖 접근 불가.
- markdown 1차 sanitize(raw HTML/이벤트핸들러/javascript: 무력화) — FE 렌더러가 최종 방어선.
- 로그/응답/이벤트 payload 에 token/secret/host 절대경로 미노출(마스킹).
- cmux 는 shell 문자열이 아니라 subprocess argument 배열로 실행.

## 알려진 제약 / 후속 (PM·DevOps·Architect 인계)

1. **PostgreSQL 미가동 시** DB 의존 엔드포인트는 503 으로 graceful 처리. 개발용 PG 구동·migration
   적용 방식은 DevOps 인계(DS-20 §15.5). 방(room) 은 디스커버리/로그수집/송신 시 (project_id, role)
   기준으로 **자동 upsert** 되므로 별도 시드 불필요.
2. **PPTX/DOCX 변환기 미구현**: `converted_preview` 는 현재 `conversion_pending`/`render_pending` 반환.
3. **로그 tee 연동(아틀라스/DV-60)**: `<project_root>/.agiteam/logs/<role>.log` 를 agiteam.sh 가 생성.
   로그 파일명 stem 은 정규 role_id(`PM.log`,`DeveloperBE.log`) 또는 토큰(`be.log`) 모두 인식한다.
   `project_root` 는 `projects_base_dir/<project_id>`(기본 `~/Projects/<project_id>`) 또는
   `WEBGUI_PROJECT_ROOTS_JSON` 매핑으로 해소.
4. **connection_state 는 응답 레벨 필드**(디스커버리 레지스트리 기반). DS-30 에 컬럼 없음 →
   Architect 가 DS-30/40 에 런타임 파생 필드로 문서화 또는 컬럼화 결정 필요(보고 대상).
5. **송신 게이팅 변경**: 기존 `ready_state=='ready'` 강제 → **레지스트리 connected + read-screen 핑**
   으로 변경(확정 liveness 설계). DS-40 `not_ready` 게이팅과 차이 → Architect 정정 중.
6. **절대경로 차단 정합(해소됨 · QI-WG-007)**: 과거 DS-60 §11.2 "선행 / 제거" vs DS-40 §16.4
   "절대경로→path_forbidden" 충돌은 **DS-60 정정으로 해소**되었다. ArtifactService 는 절대경로를
   `path_forbidden` 으로 차단(구현 유지).

## 신규/변경 모듈 (2026-06-07)

| 파일 | 역할 |
|---|---|
| `services/cmux_discovery.py` | tree 파서 + 역할매핑 + (project,role)→surface 레지스트리(liveness) |
| `services/log_collector.py` | `.agiteam/logs/<role>.log` tail → DB 방별 저장 |
| `services/background.py` | discovery/logtail 주기 폴링 루프 |
| `routers/projects.py` | `GET /api/webgui/projects` (DB 불요) |
| `services/cmux_adapter.py` | `tree()`/`read_screen()`/`ping()` 추가 |
| `services/pm_bridge.py` | PM surface 동적 해소 + 핑 + project_id 스코프 |
