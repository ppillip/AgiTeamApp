//! WG-MSG-02 send_message (PM 브리지). 웹 발신은 항상 PM surface 로 전달.
//! 레퍼런스: Python services/pm_bridge.py PMBridge.send.
//! cmux 상호작용은 MuxPort 포트로 추상화(transport 무관) — infra(agiteamapp-mux)가 구현.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::events::EventPublisher;
use crate::repo::{ApiError, NewEvent, NewMessage, WebguiRepository};

pub const PM_ROLE_ID: &str = "PM";

/// PM surface 해소 결과.
#[derive(Debug, Clone)]
pub struct PmTarget {
    pub surface_id: String,
    pub workspace_id: Option<String>,
    pub display_name: String,
}

/// cmux 멀티플렉서 포트 (outbound). agiteamapp-mux 가 구현.
pub trait MuxPort: Send + Sync {
    /// PM surface 동적 해소. 미연결이면 None.
    async fn resolve_pm(&self, project_id: &str) -> Result<Option<PmTarget>, ApiError>;
    /// read-screen 핑으로 liveness 확정.
    async fn ping(&self, target: &PmTarget) -> bool;
    /// 텍스트 제출(입력+Enter). 제출 성공 여부 반환.
    async fn submit(&self, target: &PmTarget, text: &str) -> Result<bool, ApiError>;
}

#[derive(Debug, Clone, Deserialize)]
pub struct SendMessageRequest {
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub client_message_id: Option<String>,
    // 주: attachments 는 AttachmentService(첨부 묶음) 선행 필요 — B단계 텍스트 전송만(후속 TODO).
}

fn provenance_pm_bridge() -> Value {
    json!({
        "origin": "pm_bridge",
        "is_real_data": true,
        "is_mock": false,
        "runtime_state": "live",
        "transport": "rest",
    })
}

/// 송신: PM 해소→핑→방 upsert→pending 선저장→cmux submit→status/event→WS publish.
pub async fn send_message<R: WebguiRepository, M: MuxPort, P: EventPublisher>(
    repo: &R,
    mux: &M,
    publisher: &P,
    project_id: &str,
    new_correlation_id: &str,
    req: SendMessageRequest,
) -> Result<Value, ApiError> {
    let clean = req.text.trim().to_string();
    if clean.is_empty() {
        return Err(ApiError::new("empty_message", 422, "Message text is empty."));
    }

    // 1) PM surface 해소 (refresh-before-fail 은 adapter 내부 책임).
    let target = mux
        .resolve_pm(project_id)
        .await?
        .ok_or_else(|| ApiError::new("surface_not_found", 409, "No active cmux surface for target role."))?;

    // 2) liveness 핑
    if !mux.ping(&target).await {
        return Err(ApiError::new("surface_not_found", 409, "PM surface ping failed."));
    }

    // 3) PM 방 upsert
    let room = repo
        .upsert_room(project_id, PM_ROLE_ID, &target.display_name, "pm", None, None)
        .await?;

    // Python pm_bridge: outbound message.team_session_id = room.team_session_id (DV-41 provenance).
    // upsert 는 team_session 을 COALESCE 보존하므로 방의 현재 team_session 을 읽어 싣는다.
    let team_session = repo
        .get_room_full(&room.room_id)
        .await?
        .and_then(|r| r.team_session_id);

    // 4) outbound pending 선저장 (공개 text = 사용자 원문)
    let msg = repo
        .create_message(NewMessage {
            room_id: room.room_id.clone(),
            agent_session_id: None,
            correlation_id: Some(new_correlation_id.to_string()),
            role_id: PM_ROLE_ID.to_string(),
            surface_id: Some(target.surface_id.clone()),
            team_session_id: team_session.clone(),
            direction: "outbound".to_string(),
            source: "webgui".to_string(),
            message_type: "user_message".to_string(),
            provider: None,
            transcript_path: None,
            transcript_offset: None,
            transcript_record_id: None,
            raw_text: Some(clean.clone()),
            normalized_text: clean.clone(),
            raw_hash: None, // Python outbound 정합 (dedup 비대상)
            status: "pending".to_string(),
            occurred_at_iso: repo.server_now().await?,
        })
        .await?;

    // 5) cmux submit (DB 트랜잭션 밖)
    let submitted = mux.submit(&target, &clean).await.unwrap_or(false);
    let status = if submitted { "sent" } else { "failed" };

    // 6) status 반영 + cmux_send_result event + last_message touch
    repo.set_message_status(&msg.message_id, status).await?;
    repo.touch_room_last_message(&room.room_id, &msg.message_id, &msg.occurred_at, false)
        .await?;
    repo.insert_runtime_event(NewEvent {
        room_id: room.room_id.clone(),
        event_type: "cmux_send_result".to_string(),
        source: "cmux_adapter".to_string(),
        hook_provider: None,
        hook_event_name: None,
        severity: if submitted { "info" } else { "error" }.to_string(),
        payload_json: Some(crate::masking::mask_payload(&json!({
            "submitted": submitted,
            "surface_id": target.surface_id,
            "workspace_id": target.workspace_id,
        }))),
        correlation_id: Some(new_correlation_id.to_string()),
        message_id: Some(msg.message_id.clone()),
        agent_session_id: None,
        occurred_at_iso: None,
    })
    .await?;

    // 공개 message (status 반영본)
    let mut message = crate::message::message_to_dict(&msg, project_id, Some("rest"));
    message["status"] = json!(status);
    message["client_message_id"] = json!(req.client_message_id);

    // WS publish
    let ws = json!({
        "type": "message_update",
        "cursor": format!("{}|message:{}", msg.recorded_at, msg.message_id),
        "data": {
            "update_id": format!("message:{}", msg.message_id),
            "room_id": room.room_id,
            "correlation_id": new_correlation_id,
            "update_type": if submitted { "message_sent" } else { "message_failed" },
            "message": message,
            "event": Value::Null,
            "occurred_at": msg.occurred_at,
        },
    });
    publisher.publish(&room.room_id, ws, project_id);

    let ack = json!({
        "accepted": true,
        "send_submitted": submitted,
        "message_id": msg.message_id,
        "correlation_id": new_correlation_id,
        "project_id": project_id,
        "room_id": room.room_id,
        "role": PM_ROLE_ID,
        "surface_id": target.surface_id,
        "workspace_id": target.workspace_id,
        "agent_session_id": Value::Null,
        "status": status,
        // Python ack.submitted_at = cmux submit 완료 시각(result.ended_at). normalize 대상.
        "submitted_at": msg.occurred_at,
        "provenance": provenance_pm_bridge(),
        "client_message_id": req.client_message_id,
    });

    if !submitted {
        return Err(ApiError::new("send_failed", 502, "cmux send failed."));
    }

    Ok(json!({ "ack": ack, "message": message }))
}
