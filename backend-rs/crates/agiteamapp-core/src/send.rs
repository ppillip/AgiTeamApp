//! WG-MSG-02 send_message (PM 브리지). 웹 발신은 항상 PM surface 로 전달.
//! 레퍼런스: Python services/pm_bridge.py PMBridge.send.
//! cmux 상호작용은 MuxPort 포트로 추상화(transport 무관) — infra(agiteamapp-mux)가 구현.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::attachments::AttachmentService;
use crate::events::EventPublisher;
use crate::repo::{ApiError, NewEvent, NewMessage, WebguiRepository};

pub const PM_ROLE_ID: &str = "PM";

/// FE body.attachments 항목 (사전 업로드된 attachment_id 참조, 순서 보존).
#[derive(Debug, Clone, Deserialize)]
pub struct AttachmentRef {
    pub attachment_id: String,
}

/// cmux 제출 텍스트 합성. PM(Claude Code)이 첨부 이미지를 실제로 읽을 수 있도록
/// 제출 텍스트에 첨부 이미지의 '절대 파일경로'를 함께 싣는다.
/// 형식: (사용자 텍스트 있으면 그 텍스트) + `[첨부 이미지 N개]` 헤더 + 절대경로 N줄(순서 보존).
/// 첨부 없으면 user_text 를 그대로 반환(기존 동작 불변).
pub fn compose_submit_text(user_text: &str, abs_paths: &[String]) -> String {
    let text = user_text.trim();
    if abs_paths.is_empty() {
        return text.to_string();
    }
    let mut lines: Vec<String> = Vec::new();
    if !text.is_empty() {
        lines.push(text.to_string());
    }
    lines.push(format!("[첨부 이미지 {}개]", abs_paths.len()));
    for p in abs_paths {
        lines.push(p.clone());
    }
    lines.join("\n")
}

/// PM surface 해소 결과.
#[derive(Debug, Clone)]
pub struct PmTarget {
    /// AGITEAM_HOME(team CLI 프로젝트 컨텍스트) 해소용. ping/submit 이 사용.
    pub project_id: String,
    pub surface_id: String,
    pub workspace_id: Option<String>,
    pub display_name: String,
}

/// 멀티플렉서 포트 (outbound). agiteamapp-mux 가 구현. core 는 native(cmux/tmux) 포맷을 모른다.
pub trait MuxPort: Send + Sync {
    /// 역할 surface 동적 해소(PM 포함 임의 역할). 미연결이면 None.
    async fn resolve_role(&self, project_id: &str, role: &str) -> Result<Option<PmTarget>, ApiError>;
    /// read-screen 핑으로 liveness 확정.
    async fn ping(&self, target: &PmTarget) -> bool;
    /// 텍스트 제출(입력+Enter). 제출 성공 여부 반환.
    async fn submit(&self, target: &PmTarget, text: &str) -> Result<bool, ApiError>;
    /// 멀티플렉서 인벤토리 조회. 어댑터가 native tree 를 파싱해 중립 추상 구조로 반환한다.
    /// discovery 갱신의 단일 소스(직접 native 호출 금지). 미접근 더미는 빈 목록.
    async fn tree(&self) -> Result<Vec<crate::discovery::MuxWorkspace>, ApiError>;
}

#[derive(Debug, Clone, Deserialize)]
pub struct SendMessageRequest {
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub client_message_id: Option<String>,
    // 송신 대상 역할. 미지정 시 PM(기존 계약 보존, FE 미사용 시 동작 불변).
    // 지정 시 그 역할 surface 로 보내고 그 방에 즉시 기록(응답 실패해도 '보냄' 보존, #13).
    #[serde(default)]
    pub target_role: Option<String>,
    // WG-MSG-06 첨부: 사전 업로드한 [{attachment_id}] (순서 보존). 있으면 빈 텍스트 허용(DS-40 §7.5).
    #[serde(default)]
    pub attachments: Vec<AttachmentRef>,
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
#[allow(clippy::too_many_arguments)]
pub async fn send_message<R: WebguiRepository, M: MuxPort, P: EventPublisher>(
    repo: &R,
    mux: &M,
    publisher: &P,
    project_id: &str,
    new_correlation_id: &str,
    req: SendMessageRequest,
    // WG-MSG-06: 첨부 해소용(절대경로·공개메타). http 가 project_root 로 생성해 주입. 첨부 없으면 미사용.
    attachment_service: Option<&AttachmentService>,
    now_epoch: i64,
) -> Result<Value, ApiError> {
    let clean = req.text.trim().to_string();

    // WG-MSG-06: 첨부 ID(순서 보존). 첨부가 있으면 빈 텍스트 허용(DS-40 §7.5).
    let attachment_ids: Vec<String> = req
        .attachments
        .iter()
        .map(|a| a.attachment_id.trim().to_string())
        .filter(|id| !id.is_empty())
        .collect();
    // 빈텍스트 가드 완화: 텍스트도 없고 AND 첨부도 없을 때만 422.
    if clean.is_empty() && attachment_ids.is_empty() {
        return Err(ApiError::new("empty_message", 422, "Message text is empty."));
    }

    // 첨부 절대경로 해소(부분송신 금지: 하나라도 만료/없음이면 전체 실패, §5.4.6).
    // 공개 attachments_json(말풍선용)과 cmux 제출용 절대경로를 분리 수집한다.
    let mut resolved_abs_paths: Vec<String> = Vec::new();
    let mut public_attachments: Vec<Value> = Vec::new();
    if !attachment_ids.is_empty() {
        let svc = attachment_service.ok_or_else(|| {
            ApiError::new("attachment_unavailable", 500, "Attachment service unavailable.")
        })?;
        for aid in &attachment_ids {
            let (abs, public) = svc.resolve_with_meta(project_id, aid, now_epoch)?;
            resolved_abs_paths.push(abs.to_string_lossy().to_string());
            public_attachments.push(public);
        }
    }

    // 송신 대상 역할: 미지정 시 PM(기존 계약). 지정 시 해당 에이전트로 직접 전송.
    let role = req
        .target_role
        .clone()
        .map(|r| r.trim().to_string())
        .filter(|r| !r.is_empty())
        .unwrap_or_else(|| PM_ROLE_ID.to_string());
    let room_type = if role == PM_ROLE_ID { "pm" } else { "role" };

    // 1) 대상 역할 surface 해소 (refresh-before-fail 은 adapter 내부 책임).
    let target = mux
        .resolve_role(project_id, &role)
        .await?
        .ok_or_else(|| ApiError::new("surface_not_found", 409, "No active cmux surface for target role."))?;

    // 2) liveness 핑
    if !mux.ping(&target).await {
        return Err(ApiError::new("surface_not_found", 409, "Target surface ping failed."));
    }

    // 3) 대상 역할 방 upsert
    let room = repo
        .upsert_room(project_id, &role, &target.display_name, room_type, None, None)
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
            role_id: role.clone(),
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
            // 공개 attachments_json(말풍선용). 첨부 없으면 None → DB NULL.
            attachments: if public_attachments.is_empty() {
                None
            } else {
                Some(json!(public_attachments))
            },
        })
        .await?;

    // 5) cmux submit (DB 트랜잭션 밖). 첨부 있으면 본문 뒤에 절대경로 블록을 합성해
    //    PM(Claude Code)이 그 경로를 Read 로 열어 이미지를 확인할 수 있게 한다.
    let submit_text = compose_submit_text(&clean, &resolved_abs_paths);
    if !resolved_abs_paths.is_empty() {
        // 운영 진단: cmux 제출 텍스트에 첨부 절대경로가 실제 포함됐는지 확인.
        eprintln!(
            "[send] attachments={} cmux submit_text={submit_text:?}",
            resolved_abs_paths.len()
        );
    }
    let submitted = mux.submit(&target, &submit_text).await.unwrap_or(false);
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
        "role": role,
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
