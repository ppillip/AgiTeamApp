-- RV-60 parity deterministic seed.
--
-- Applied to both agiteamapp_equiv_py and agiteamapp_equiv_rs after the same
-- migrations. IDs and timestamps are intentionally fixed so RV-60 can remove
-- placeholder-driven 500s and compare Python/Rust side effects reproducibly.

BEGIN;

-- Stable rooms: PM is required by WG-MSG-02 fake mux send, QA is used by read
-- and collector contracts.
INSERT INTO webgui_room (
    room_id, project_id, role_id, display_name, agent_type, room_type,
    current_surface_id, ready_state, team_session_id, agent_id,
    last_message_at, read_marker_at, unread_count, created_at, updated_at
) VALUES
    (
        '00000000-0000-4000-8000-000000000100', 'Panthea', 'PM', '제우스',
        'pm', 'pm', 'surface:equiv-pm', 'ready', 'rv60-team-session',
        'rv60-pm-agent', '2026-06-16T08:59:00Z', NULL, 0,
        '2026-06-16T08:55:00Z', '2026-06-16T08:59:00Z'
    ),
    (
        '00000000-0000-4000-8000-000000000101', 'Panthea', 'QA', '아르고스',
        'codex', 'role', 'surface:equiv-qa', 'ready', 'rv60-team-session',
        'rv60-qa-agent', '2026-06-16T09:01:00Z', NULL, 2,
        '2026-06-16T08:55:00Z', '2026-06-16T09:01:00Z'
    );

INSERT INTO webgui_agent_session (
    agent_session_id, room_id, role_id, surface_id, agent_type,
    ready_state, collector_state, started_at, ended_at, last_seen_at,
    created_at, updated_at
) VALUES
    (
        '00000000-0000-4000-8000-000000000201',
        '00000000-0000-4000-8000-000000000101',
        'QA', 'surface:equiv-qa', 'codex', 'ready', 'unknown',
        '2026-06-16T08:55:00Z', NULL, '2026-06-16T09:01:10Z',
        '2026-06-16T08:55:00Z', '2026-06-16T09:01:10Z'
    );

INSERT INTO webgui_message (
    message_id, room_id, agent_session_id, correlation_id, role_id, surface_id,
    team_session_id, direction, source, message_type, provider, transcript_path,
    transcript_offset, transcript_record_id, raw_text, normalized_text, raw_hash,
    status, occurred_at, recorded_at, updated_at, attachments_json
) VALUES
    (
        '00000000-0000-4000-8000-000000000301',
        '00000000-0000-4000-8000-000000000101',
        '00000000-0000-4000-8000-000000000201',
        '00000000-0000-4000-8000-000000000501',
        'QA', 'surface:equiv-qa', 'rv60-team-session',
        'outbound', 'webgui', 'user_message', NULL, NULL, NULL, NULL,
        'RV60 seeded user prompt', 'RV60 seeded user prompt',
        'seed:rv60:user:001', 'sent',
        '2026-06-16T09:00:00Z', '2026-06-16T09:00:00Z',
        '2026-06-16T09:00:01Z', NULL
    ),
    (
        '00000000-0000-4000-8000-000000000302',
        '00000000-0000-4000-8000-000000000101',
        '00000000-0000-4000-8000-000000000201',
        '00000000-0000-4000-8000-000000000501',
        'QA', 'surface:equiv-qa', 'rv60-team-session',
        'inbound', 'transcript', 'assistant_message', 'claude_code',
        'fixtures/transcripts/rv40-stop.jsonl', '1', 'rv60-transcript-seed-001',
        'RV60 seeded assistant reply', 'RV60 seeded assistant reply',
        'seed:rv60:assistant:001', 'received',
        '2026-06-16T09:01:00Z', '2026-06-16T09:01:00Z',
        '2026-06-16T09:01:01Z',
        '[{"attachment_id":"att_0000000000004000800000000000601","kind":"image","filename":"upload-20260616T090000Z-rv60.png","mime_type":"image/png","size_bytes":67,"width":1,"height":1,"sha256":"seed-rv60-preview","preview_url":"/api/webgui/message-attachments/att_0000000000004000800000000000601/preview","expires_at":"2026-06-17T09:00:00Z"}]'::jsonb
    );

INSERT INTO webgui_runtime_event (
    event_id, room_id, agent_session_id, message_id, correlation_id, event_type,
    source, hook_provider, hook_event_name, severity, payload_json,
    masked_payload_json, occurred_at, recorded_at
) VALUES
    (
        '00000000-0000-4000-8000-000000000401',
        '00000000-0000-4000-8000-000000000101',
        '00000000-0000-4000-8000-000000000201',
        '00000000-0000-4000-8000-000000000302',
        '00000000-0000-4000-8000-000000000501',
        'hook_stop', 'hook', 'claude_code', 'Stop', 'info',
        '{"session_id":"rv60-session","transcript_path":"fixtures/transcripts/rv40-stop.jsonl","agent_id":"rv60-qa-agent"}'::jsonb,
        '{"session_id":"rv60-session","transcript_path":"fixtures/transcripts/rv40-stop.jsonl","agent_id":"rv60-qa-agent"}'::jsonb,
        '2026-06-16T09:01:30Z', '2026-06-16T09:01:30Z'
    );

UPDATE webgui_room
   SET current_agent_session_id = '00000000-0000-4000-8000-000000000201',
       last_message_id = '00000000-0000-4000-8000-000000000302',
       last_message_at = '2026-06-16T09:01:00Z'
 WHERE room_id = '00000000-0000-4000-8000-000000000101';

COMMIT;
