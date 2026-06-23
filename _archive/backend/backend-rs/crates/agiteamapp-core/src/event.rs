//! WG-CHAT-06: runtime_event 저장 (+ WS publish / hook_stop transcript 트리거). 레퍼런스: collect_event.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::events::EventPublisher;
use crate::repo::{
    normalize_event_type, ApiError, EventRow, NewEvent, TranscriptHint, WebguiRepository,
};
use crate::transcript::TranscriptPort;

#[derive(Debug, Clone, Deserialize)]
pub struct CollectEventRequest {
    #[serde(default)]
    pub agent_session_id: Option<String>,
    #[serde(default)]
    pub message_id: Option<String>,
    #[serde(default)]
    pub correlation_id: Option<String>,
    #[serde(default)]
    pub event_type: Option<String>,
    pub source: String,
    #[serde(default)]
    pub hook_provider: Option<String>,
    #[serde(default)]
    pub hook_event_name: Option<String>,
    #[serde(default = "default_severity")]
    pub severity: String,
    #[serde(default)]
    pub payload: Option<Value>,
    pub occurred_at: String,
}

fn default_severity() -> String {
    "info".to_string()
}

/// runtime_event 공개 응답 (Python event_to_dict 정합).
pub fn event_to_dict(e: &EventRow) -> Value {
    json!({
        "event_id": e.event_id,
        "room_id": e.room_id,
        "message_id": e.message_id,
        "correlation_id": e.correlation_id,
        "event_type": e.event_type,
        "source": e.source,
        "hook_provider": e.hook_provider,
        "hook_event_name": e.hook_event_name,
        "severity": e.severity,
        "payload": e.payload,
        "occurred_at": e.occurred_at,
    })
}

pub async fn collect_event<R: WebguiRepository, P: EventPublisher, T: TranscriptPort>(
    repo: &R,
    publisher: &P,
    transcript: &T,
    room_id: &str,
    req: CollectEventRequest,
) -> Result<Value, ApiError> {
    let room = repo
        .get_room(room_id)
        .await?
        .ok_or_else(|| ApiError::new("room_not_found", 404, "Room not found."))?;

    // hook source 면 event_type 정규화 (Python: hook_normalizer)
    let mut event_type = req.event_type.clone().unwrap_or_default();
    if req.source == "hook" {
        if let Some(provider) = req.hook_provider.as_deref() {
            if event_type.is_empty() {
                event_type =
                    normalize_event_type(provider, req.hook_event_name.as_deref().unwrap_or(""));
            }
        }
    }
    if event_type.is_empty() {
        return Err(ApiError::new(
            "invalid_event_type",
            422,
            "event_type or hook_provider required.",
        ));
    }

    // payload masking (secret 키/토큰/홈경로)
    let masked = req.payload.as_ref().map(crate::masking::mask_payload);
    let ev = repo
        .insert_runtime_event(NewEvent {
            room_id: room.room_id.clone(),
            event_type: event_type.clone(),
            source: req.source.clone(),
            hook_provider: req.hook_provider.clone(),
            hook_event_name: req.hook_event_name.clone(),
            severity: req.severity.clone(),
            payload_json: masked,
            correlation_id: req.correlation_id.clone(),
            message_id: req.message_id.clone(),
            agent_session_id: req.agent_session_id.clone(),
            occurred_at_iso: Some(req.occurred_at.clone()),
        })
        .await?;

    // WS publish — hook_stop→correlation_closed / error→runtime_error fanout (Python 동등).
    let update_type = if event_type == "hook_stop" {
        Some("correlation_closed")
    } else if req.severity == "error" {
        Some("runtime_error")
    } else {
        None
    };
    if let Some(ut) = update_type {
        let ws = json!({
            "type": "message_update",
            "cursor": format!("{}|event:{}", ev.recorded_at, ev.event_id),
            "data": {
                "update_id": format!("event:{}", ev.event_id),
                "room_id": ev.room_id,
                "correlation_id": ev.correlation_id,
                "update_type": ut,
                "message": Value::Null,
                "event": { "event_type": ev.event_type, "source": ev.source, "severity": ev.severity },
                "occurred_at": ev.occurred_at,
            },
        });
        publisher.publish(&room.room_id, ws, &room.project_id);
    }

    // hook_stop → transcript 즉시수집 트리거
    if event_type == "hook_stop" {
        let hint = TranscriptHint {
            provider: req.hook_provider.clone(),
            session_id: req.payload.as_ref().and_then(|p| p.get("session_id")).and_then(|v| v.as_str()).map(|s| s.to_string()),
            transcript_path: req.payload.as_ref().and_then(|p| p.get("transcript_path")).and_then(|v| v.as_str()).map(|s| s.to_string()),
            agent_id: None,
            project_id: room.project_id.clone(),
            role: room.role_id.clone(),
            room_id: room.room_id.clone(),
        };
        transcript.collect(&hint).await;
    }

    Ok(json!({ "event": event_to_dict(&ev) }))
}
