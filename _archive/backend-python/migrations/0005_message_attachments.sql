-- 0005_message_attachments.sql
-- 이미지 첨부 참조 (DV-90 / 요구사항 16-1, DS-120). webgui_message.attachments_json 추가.
-- 공개 MessageAttachment 메타(attachment_id/filename/mime/size/w/h/sha256/preview_url/expires_at)
-- 목록만 저장한다. host 절대경로는 저장하지 않으며 PM Bridge 내부 resolve 에서만 사용한다.
-- 멱등(재실행 안전).

BEGIN;

ALTER TABLE webgui_message ADD COLUMN IF NOT EXISTS attachments_json jsonb;

COMMIT;
