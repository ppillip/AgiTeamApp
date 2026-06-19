-- 0006_runtime_activity_poller_source.sql
-- 요구사항 15-1 (B)폴러 모델: read-screen poller 가 active pulse 를 runtime_event 로 저장한다
-- (event_type='runtime_activity_changed', source='read_screen_poller', DS-110 §6.2/§10.1).
-- 기존 ck_webgui_event_source CHECK 화이트리스트에 'read_screen_poller' 가 없어 INSERT 가
-- CheckViolation → 503(Storage unavailable)으로 떨어졌다. 허용값에 추가한다.
-- event_type 은 CHECK 제약이 없어 'runtime_activity_changed' 는 별도 조치 불요.
-- last_active_at 등은 payload_json(JSONB) 안에 저장하므로 신규 컬럼 불요.
-- 멱등(재실행 안전).

BEGIN;

ALTER TABLE webgui_runtime_event DROP CONSTRAINT IF EXISTS ck_webgui_event_source;
ALTER TABLE webgui_runtime_event ADD CONSTRAINT ck_webgui_event_source CHECK (
    source IN (
        'cmux_adapter','conversation_collector','transcript_parser','raw_log_collector',
        'hook','read_screen','backend','artifact_service','postgres_notify','cmux_discovery',
        'read_screen_poller'
    )
);

COMMIT;
