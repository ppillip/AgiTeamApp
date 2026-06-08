-- 0002_room_routing_team_session_agent.sql
-- 방 라우팅 안정키 정합 (DS-40 v0.13 / DS-60 v0.11): project_id + team_session_id + agent_id.
-- QI-WG-015 roomless 발사 정합 — roomless hook upsert 키를 team_session_id + agent_id 로 보정.
-- role_id 는 표시·검증 metadata 로 격하. team_session_id 는 재부팅 간 방 충돌 차단 기준.
-- 멱등(idempotent): IF NOT EXISTS / IF EXISTS 로 재실행 안전.

BEGIN;

-- 1) 안정키 컬럼 추가 (legacy role 방은 NULL 허용)
ALTER TABLE webgui_room ADD COLUMN IF NOT EXISTS team_session_id text;
ALTER TABLE webgui_room ADD COLUMN IF NOT EXISTS agent_id        text;

-- 2) 구 유일성(project_id, role_id) 제거 — 재부팅(team_session 변경) 시 방 충돌 차단을 위해 폐기
ALTER TABLE webgui_room DROP CONSTRAINT IF EXISTS uk_webgui_room_project_role;

-- 3) 안정키 유일성 (project_id, team_session_id, agent_id).
--    NULL 은 distinct → team_session_id/agent_id 미지정(legacy role 방)은 충돌하지 않는다.
CREATE UNIQUE INDEX IF NOT EXISTS uk_webgui_room_team_agent
    ON webgui_room (project_id, team_session_id, agent_id);

-- 4) 조회 보조 인덱스
CREATE INDEX IF NOT EXISTS idx_webgui_room_agent
    ON webgui_room (agent_id) WHERE agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webgui_room_team_session
    ON webgui_room (team_session_id) WHERE team_session_id IS NOT NULL;

COMMIT;
