//! WG-CHAT-05: collector 메시지 저장 (dedupe + correlation). 레퍼런스: collect_message.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::events::EventPublisher;
use crate::masking::{mask_text, sanitize_tool_leak};
use crate::repo::{compute_raw_hash, ApiError, MessageRow, NewMessage, WebguiRepository};

const MESSAGE_SOURCES: &[&str] = &["bridge", "hook", "transcript", "webgui", "pm_bridge"];
/// provenance 실데이터 인정 origin (Python _REAL_SOURCES).
const REAL_SOURCES: &[&str] = &["hook", "transcript", "bridge", "pm_bridge", "webgui"];

#[derive(Debug, Clone, Deserialize)]
pub struct CollectMessageRequest {
    #[serde(default)]
    pub agent_session_id: Option<String>,
    pub role_id: String,
    #[serde(default)]
    pub surface_id: Option<String>,
    pub source: String,
    pub message_type: String,
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default)]
    pub transcript_path: Option<String>,
    #[serde(default)]
    pub transcript_offset: Option<String>,
    #[serde(default)]
    pub transcript_record_id: Option<String>,
    #[serde(default)]
    pub raw_text: Option<String>,
    pub normalized_text: String,
    #[serde(default)]
    pub raw_hash: Option<String>,
    #[serde(default)]
    pub correlation_id: Option<String>,
    pub occurred_at: String,
}

/// 공개 메시지 응답 (Python message_to_dict 정합). project_id 는 호출처(room) 주입.
pub fn message_to_dict(m: &MessageRow, project_id: &str, transport: Option<&str>) -> Value {
    let origin = if m.source.is_empty() { "mock" } else { m.source.as_str() };
    let is_real = REAL_SOURCES.contains(&origin);
    // Python provenance_dict 정합: origin/runtime_state/is_real_data/is_mock(+transport 선택).
    let mut provenance = json!({
        "origin": origin,
        "runtime_state": if is_real { "live" } else { "mock" },
        "is_real_data": is_real,
        "is_mock": origin == "mock",
    });
    if let Some(t) = transport {
        provenance["transport"] = json!(t);
    }
    json!({
        "message_id": m.message_id,
        "project_id": project_id,
        "room_id": m.room_id,
        "correlation_id": m.correlation_id,
        "role": m.role_id,
        "surface_id": m.surface_id,
        "agent_session_id": m.agent_session_id,
        "team_session_id": m.team_session_id,
        "direction": m.direction,
        "source": m.source,
        "message_type": m.message_type,
        "text": m.normalized_text,
        // DS-40 §4.2.1 이미지 첨부 공개 메타(절대경로 미포함). 없으면 [].
        "attachments": m.attachments_json,
        "status": m.status,
        "provenance": provenance,
        "occurred_at": m.occurred_at,
        "recorded_at": m.recorded_at,
        "updated_at": m.updated_at,
    })
}

pub async fn collect_message<R: WebguiRepository, P: EventPublisher>(
    repo: &R,
    publisher: &P,
    room_id: &str,
    req: CollectMessageRequest,
) -> Result<Value, ApiError> {
    let room = repo
        .get_room(room_id)
        .await?
        .ok_or_else(|| ApiError::new("room_not_found", 404, "Room not found."))?;

    if req.role_id != room.role_id {
        return Err(ApiError::new("room_role_mismatch", 409, "role_id does not match room."));
    }
    if !MESSAGE_SOURCES.contains(&req.source.as_str()) {
        return Err(ApiError::new("invalid_source", 422, "Unsupported message source."));
    }
    // 주: Python 은 agent_session_id 소유 검증을 추가로 한다. B단계는 생략(후속 TODO).

    let raw_hash = req.raw_hash.clone().unwrap_or_else(|| {
        compute_raw_hash(
            req.provider.as_deref(),
            req.transcript_record_id.as_deref(),
            &room.role_id,
            &req.normalized_text,
        )
    });

    // dedupe 1: (provider, record_id)
    if let (Some(p), Some(rec)) = (req.provider.as_deref(), req.transcript_record_id.as_deref()) {
        if let Some(existing) = repo.find_message_by_record(p, rec).await? {
            return Ok(json!({ "message": message_to_dict(&existing, &room.project_id, Some("websocket")), "deduplicated": true }));
        }
    }
    // dedupe 2: (room, source, raw_hash)
    if let Some(existing) = repo.find_message_by_hash(&room.room_id, &req.source, &raw_hash).await? {
        return Ok(json!({ "message": message_to_dict(&existing, &room.project_id, Some("websocket")), "deduplicated": true }));
    }

    let is_inbound = req.message_type == "assistant_message" || req.message_type == "unmatched";
    let direction = if is_inbound { "inbound" } else { "outbound" };

    // correlation 매칭
    let mut correlation_id = req.correlation_id.clone();
    let mut status = if is_inbound { "received".to_string() } else { "sent".to_string() };
    let mut message_type = req.message_type.clone();
    if is_inbound && correlation_id.is_none() {
        match repo.find_open_outbound_correlation(&room.room_id).await? {
            Some(cid) => correlation_id = Some(cid),
            None => {
                status = "unmatched".to_string();
                message_type = "unmatched".to_string();
            }
        }
    }

    let new_msg = NewMessage {
        room_id: room.room_id.clone(),
        agent_session_id: req.agent_session_id.clone(),
        correlation_id: correlation_id.clone(),
        role_id: room.role_id.clone(),
        surface_id: req.surface_id.clone(),
        team_session_id: room.team_session_id.clone(),
        direction: direction.to_string(),
        source: req.source.clone(),
        message_type,
        provider: req.provider.clone(),
        transcript_path: mask_text(req.transcript_path.as_deref()),
        transcript_offset: req.transcript_offset.clone(),
        transcript_record_id: req.transcript_record_id.clone(),
        // raw_text=mask_text(원본), normalized=sanitize_tool_leak(표시본). raw_hash 는 원본 기준(위에서 계산, dedup 불변).
        raw_text: mask_text(req.raw_text.as_deref()),
        normalized_text: sanitize_tool_leak(Some(&req.normalized_text)).unwrap_or_default(),
        raw_hash: Some(raw_hash),
        status,
        occurred_at_iso: req.occurred_at.clone(),
        attachments: None,
    };
    let msg = repo.create_message(new_msg).await?;
    repo.touch_room_last_message(&room.room_id, &msg.message_id, &msg.occurred_at, is_inbound)
        .await?;

    let payload = message_to_dict(&msg, &room.project_id, Some("websocket"));
    // WS publish — 방 구독자에게 message_received/message_sent fanout (Python 동등).
    let ws = json!({
        "type": "message_update",
        "cursor": format!("{}|message:{}", msg.recorded_at, msg.message_id),
        "data": {
            "update_id": format!("message:{}", msg.message_id),
            "room_id": msg.room_id,
            "correlation_id": correlation_id,
            "update_type": if is_inbound { "message_received" } else { "message_sent" },
            "message": payload,
            "event": Value::Null,
            "occurred_at": msg.occurred_at,
        },
    });
    publisher.publish(&room.room_id, ws, &room.project_id);

    Ok(json!({ "message": message_to_dict(&msg, &room.project_id, Some("websocket")), "deduplicated": false }))
}
