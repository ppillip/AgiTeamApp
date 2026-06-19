-- 0004_message_team_session_provenance.sql
-- 메시지 provenance: webgui_message.team_session_id 추가 (DV-41).
-- FE 가 메시지별 팀 부팅 세션을 알아 세션 구분선을 그릴 수 있게 한다(현재 방의 값이 아니라
-- 메시지 생성 시점에 고정된 값). 멱등(재실행 안전).

BEGIN;

ALTER TABLE webgui_message ADD COLUMN IF NOT EXISTS team_session_id text;

CREATE INDEX IF NOT EXISTS idx_webgui_message_team_session
    ON webgui_message (room_id, team_session_id);

-- DV-42: cmux 디스커버리 연결상태 변경 이벤트 source 'cmux_discovery' 허용.
-- (background discovery_loop 가 room_connection_changed 이벤트를 이 source 로 저장한다)
ALTER TABLE webgui_runtime_event DROP CONSTRAINT IF EXISTS ck_webgui_event_source;
ALTER TABLE webgui_runtime_event ADD CONSTRAINT ck_webgui_event_source CHECK (
    source IN (
        'cmux_adapter','conversation_collector','transcript_parser','raw_log_collector',
        'hook','read_screen','backend','artifact_service','postgres_notify','cmux_discovery'
    )
);

COMMIT;
