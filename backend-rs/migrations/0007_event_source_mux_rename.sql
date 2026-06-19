-- 0007_event_source_mux_rename.sql
-- P2(transport 추상화 — backend-rs 가 멀티플렉서 직접 의존을 제거) 후속 핫픽스.
--
-- 배경: backend-rs 가 runtime_event.source 를 'mux_adapter' 로 emit 하도록 코드가 바뀌었으나,
--       라이브 CHECK 제약 ck_webgui_event_source 는 과거 마이그레이션(0001/0004/0006)으로 적용된
--       구 화이트리스트(레거시 어댑터 값)만 허용했다. 그 결과 send 경로의 insert_runtime_event 가
--       CheckViolation 으로 깨져 API 가 에러를 반환 → 웹→터미널 "전송 실패".
--
-- 조치: 화이트리스트를 현행 transport-중립 값으로 교체한다(mux_adapter / mux_discovery 포함).
--       코드가 실제 emit 하는 event source(mux_adapter / hook / read_screen_poller)는 전부 포함된다.
--       mux_discovery 는 현재 코드가 emit 하지 않더라도 대칭을 위해 선반영(무방).
--
-- 전제: 구 레거시 source 값으로 적재된 역사 행은 본 strict 제약 적용 전에 현행 값으로 정리(rename)되어
--       있어야 한다. 라이브 DB 에는 1회성 운영 정리로 이미 반영했다(상세는 핫픽스 보고 참조). 신규/clean
--       DB 에는 해당 역사 행이 없으므로 본 파일만으로 곧바로 적용된다.
--
-- 주의: 기존 0001~0006 마이그레이션은 편집하지 않는다(이미 라이브 적용됨). 본 파일은 신규 델타다.
--       멱등(재실행 안전): DROP CONSTRAINT IF EXISTS 후 재생성.

BEGIN;

ALTER TABLE webgui_runtime_event DROP CONSTRAINT IF EXISTS ck_webgui_event_source;
ALTER TABLE webgui_runtime_event ADD CONSTRAINT ck_webgui_event_source CHECK (
    source = ANY (ARRAY[
        'mux_adapter','conversation_collector','transcript_parser','raw_log_collector',
        'hook','read_screen','backend','artifact_service','postgres_notify',
        'mux_discovery','read_screen_poller'
    ])
);

COMMIT;
