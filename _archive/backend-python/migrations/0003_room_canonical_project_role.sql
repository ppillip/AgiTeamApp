-- 0003_room_canonical_project_role.sql
-- 방 canonical 안정키를 (project_id, role_id) 로 복원 (DV-41 / QI-WG-022).
-- team_session_id + agent_id 키잉으로 생긴 중복 방을 (project,role)당 1개로 병합하고,
-- uk_webgui_room_team_agent 폐기 → uk_webgui_room_project_role 신설.
-- team_session_id / agent_id 컬럼은 provenance 로 유지. 멱등(재실행 안전).

BEGIN;

-- 1) (project_id, role_id) 그룹별 canonical room 선정 = 가장 먼저 생성된 방
CREATE TEMP TABLE _room_canon ON COMMIT DROP AS
SELECT
    room_id,
    first_value(room_id) OVER (
        PARTITION BY project_id, role_id ORDER BY created_at ASC, room_id ASC
    ) AS canon_id
FROM webgui_room;

-- 2) 자식 레코드를 canonical room 으로 재귀속 (이력 보존)
UPDATE webgui_message m
   SET room_id = rc.canon_id
  FROM _room_canon rc
 WHERE m.room_id = rc.room_id AND rc.room_id <> rc.canon_id;

UPDATE webgui_runtime_event e
   SET room_id = rc.canon_id
  FROM _room_canon rc
 WHERE e.room_id = rc.room_id AND rc.room_id <> rc.canon_id;

UPDATE webgui_agent_session s
   SET room_id = rc.canon_id
  FROM _room_canon rc
 WHERE s.room_id = rc.room_id AND rc.room_id <> rc.canon_id;

-- 3) 중복 방의 자기참조 FK 정리 후 삭제 (canon 아닌 방)
UPDATE webgui_room r
   SET last_message_id = NULL, current_agent_session_id = NULL
  FROM _room_canon rc
 WHERE r.room_id = rc.room_id AND rc.room_id <> rc.canon_id;

DELETE FROM webgui_room r
 USING _room_canon rc
 WHERE r.room_id = rc.room_id AND rc.room_id <> rc.canon_id;

-- 4) canonical room 의 last_message_id / last_message_at 재계산 (병합 반영)
UPDATE webgui_room r
   SET last_message_id = lm.message_id,
       last_message_at = lm.occurred_at
  FROM (
    SELECT DISTINCT ON (room_id) room_id, message_id, occurred_at
      FROM webgui_message
     ORDER BY room_id, occurred_at DESC, message_id DESC
  ) lm
 WHERE r.room_id = lm.room_id;

-- 5) unique 키 교체: team_agent 폐기 → project_role 신설
DROP INDEX IF EXISTS uk_webgui_room_team_agent;
ALTER TABLE webgui_room DROP CONSTRAINT IF EXISTS uk_webgui_room_team_agent;
CREATE UNIQUE INDEX IF NOT EXISTS uk_webgui_room_project_role
    ON webgui_room (project_id, role_id);

COMMIT;
