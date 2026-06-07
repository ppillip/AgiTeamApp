-- AgiTeamApp WebGUI 초기 스키마 (DS-30 DB설계서 기준, PostgreSQL)
-- 생성 순서: room -> agent_session -> message -> runtime_event
-- 순환 FK(room.current_agent_session_id, room.last_message_id)는 테이블 생성 후 ALTER 로 추가 (DS-30 §11.2)

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- 1) webgui_room ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webgui_room (
    room_id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                text        NOT NULL,
    role_id                   text        NOT NULL,
    display_name              text        NOT NULL,
    agent_type                text,
    room_type                 text        NOT NULL DEFAULT 'role',
    current_surface_id        text,
    current_agent_session_id  uuid,
    ready_state               text        NOT NULL DEFAULT 'unknown',
    last_message_id           uuid,
    last_message_at           timestamptz,
    read_marker_at            timestamptz,
    unread_count              integer     NOT NULL DEFAULT 0,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uk_webgui_room_project_role UNIQUE (project_id, role_id),
    CONSTRAINT ck_webgui_room_unread       CHECK (unread_count >= 0),
    CONSTRAINT ck_webgui_room_type         CHECK (room_type IN ('pm','role')),
    CONSTRAINT ck_webgui_room_ready        CHECK (ready_state IN ('unknown','ready','not_ready','offline'))
);
CREATE INDEX IF NOT EXISTS idx_webgui_room_project       ON webgui_room (project_id);
CREATE INDEX IF NOT EXISTS idx_webgui_room_role          ON webgui_room (role_id);
CREATE INDEX IF NOT EXISTS idx_webgui_room_type          ON webgui_room (room_type);
CREATE INDEX IF NOT EXISTS idx_webgui_room_surface       ON webgui_room (current_surface_id);
CREATE INDEX IF NOT EXISTS idx_webgui_room_session       ON webgui_room (current_agent_session_id);
CREATE INDEX IF NOT EXISTS idx_webgui_room_ready         ON webgui_room (ready_state);
CREATE INDEX IF NOT EXISTS idx_webgui_room_last_message  ON webgui_room (project_id, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_webgui_room_unread        ON webgui_room (project_id, unread_count);

-- 2) webgui_agent_session ---------------------------------------------------
CREATE TABLE IF NOT EXISTS webgui_agent_session (
    agent_session_id  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id           uuid        NOT NULL REFERENCES webgui_room(room_id) ON DELETE RESTRICT,
    role_id           text        NOT NULL,
    surface_id        text        NOT NULL,
    agent_type        text,
    ready_state       text        NOT NULL DEFAULT 'unknown',
    collector_state   text        NOT NULL DEFAULT 'unknown',
    started_at        timestamptz,
    ended_at          timestamptz,
    last_seen_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_webgui_session_ready     CHECK (ready_state IN ('unknown','ready','not_ready','offline')),
    CONSTRAINT ck_webgui_session_collector CHECK (collector_state IN ('unknown','running','delayed','stopped'))
);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_room      ON webgui_agent_session (room_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_role      ON webgui_agent_session (role_id);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_surface   ON webgui_agent_session (surface_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_ready     ON webgui_agent_session (ready_state);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_collector ON webgui_agent_session (collector_state);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_started   ON webgui_agent_session (started_at);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_seen      ON webgui_agent_session (last_seen_at);
CREATE INDEX IF NOT EXISTS idx_webgui_agent_session_active    ON webgui_agent_session (room_id) WHERE ended_at IS NULL;

-- 3) webgui_message ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS webgui_message (
    message_id       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id          uuid        NOT NULL REFERENCES webgui_room(room_id) ON DELETE RESTRICT,
    agent_session_id uuid        REFERENCES webgui_agent_session(agent_session_id) ON DELETE RESTRICT,
    correlation_id   uuid,
    role_id          text        NOT NULL,
    surface_id       text,
    direction        text        NOT NULL,
    source           text        NOT NULL,
    message_type     text        NOT NULL DEFAULT 'user_message',
    raw_text         text,
    normalized_text  text,
    raw_hash         text,
    status           text        NOT NULL DEFAULT 'received',
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    recorded_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_webgui_message_direction CHECK (direction IN ('outbound','inbound','system')),
    CONSTRAINT ck_webgui_message_source    CHECK (source IN ('webgui','pm_bridge','role_log','hook','read_screen')),
    CONSTRAINT ck_webgui_message_type      CHECK (message_type IN ('user_message','log_line','status','error','unmatched')),
    CONSTRAINT ck_webgui_message_status    CHECK (status IN ('pending','sent','failed','blocked','received','streaming','unmatched','closed','superseded'))
);
CREATE INDEX IF NOT EXISTS idx_webgui_message_room_time    ON webgui_message (room_id, occurred_at, message_id);
CREATE INDEX IF NOT EXISTS idx_webgui_message_room_status  ON webgui_message (room_id, status, occurred_at);
CREATE INDEX IF NOT EXISTS idx_webgui_message_correlation  ON webgui_message (correlation_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_webgui_message_session      ON webgui_message (agent_session_id);
CREATE INDEX IF NOT EXISTS idx_webgui_message_role         ON webgui_message (role_id);
CREATE INDEX IF NOT EXISTS idx_webgui_message_surface      ON webgui_message (surface_id);
CREATE INDEX IF NOT EXISTS idx_webgui_message_direction    ON webgui_message (direction);
CREATE INDEX IF NOT EXISTS idx_webgui_message_source       ON webgui_message (source);
CREATE INDEX IF NOT EXISTS idx_webgui_message_type         ON webgui_message (message_type);
CREATE INDEX IF NOT EXISTS idx_webgui_message_recorded     ON webgui_message (recorded_at);
-- role_log 중복 방지: 같은 session+source+raw_hash 유일 (raw_hash 존재 시)
CREATE UNIQUE INDEX IF NOT EXISTS idx_webgui_message_dedupe
    ON webgui_message (agent_session_id, source, raw_hash) WHERE raw_hash IS NOT NULL;

-- 4) webgui_runtime_event ---------------------------------------------------
CREATE TABLE IF NOT EXISTS webgui_runtime_event (
    event_id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id             uuid        NOT NULL REFERENCES webgui_room(room_id) ON DELETE RESTRICT,
    agent_session_id    uuid        REFERENCES webgui_agent_session(agent_session_id) ON DELETE RESTRICT,
    message_id          uuid        REFERENCES webgui_message(message_id) ON DELETE RESTRICT,
    correlation_id      uuid,
    event_type          text        NOT NULL,
    source              text        NOT NULL,
    hook_provider       text,
    hook_event_name     text,
    severity            text        NOT NULL DEFAULT 'info',
    payload_json        jsonb,
    masked_payload_json jsonb,
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_webgui_event_severity CHECK (severity IN ('debug','info','warning','error')),
    CONSTRAINT ck_webgui_event_source   CHECK (source IN ('cmux_adapter','role_log_collector','hook','read_screen','backend','artifact_service','postgres_notify'))
);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_room_time      ON webgui_runtime_event (room_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_correlation    ON webgui_runtime_event (correlation_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_type           ON webgui_runtime_event (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_source         ON webgui_runtime_event (source);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_session        ON webgui_runtime_event (agent_session_id);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_message        ON webgui_runtime_event (message_id);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_severity       ON webgui_runtime_event (severity);
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_hook_provider  ON webgui_runtime_event (hook_provider, occurred_at) WHERE hook_provider IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webgui_runtime_event_hook_name      ON webgui_runtime_event (hook_event_name, occurred_at) WHERE hook_event_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS gin_webgui_runtime_event_payload        ON webgui_runtime_event USING gin (payload_json);

-- 5) 순환 FK 후행 추가 (DS-30 §11.2) ---------------------------------------
ALTER TABLE webgui_room
    ADD CONSTRAINT fk_webgui_room_current_session
    FOREIGN KEY (current_agent_session_id) REFERENCES webgui_agent_session(agent_session_id) ON DELETE RESTRICT;
ALTER TABLE webgui_room
    ADD CONSTRAINT fk_webgui_room_last_message
    FOREIGN KEY (last_message_id) REFERENCES webgui_message(message_id) ON DELETE RESTRICT;

COMMIT;
