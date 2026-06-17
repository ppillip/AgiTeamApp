//! WG-HOOK-01: roomless hook 수집. (project_id, role) 방 upsert → runtime_event → hook_stop 트리거.
//! 레퍼런스: Python collector_service.collect_hook + collect_event.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::events::EventPublisher;
use crate::repo::{
    normalize_event_type, normalize_provider, ApiError, NewEvent, TranscriptHint, WebguiRepository,
};
use crate::transcript::TranscriptPort;

#[derive(Debug, Clone, Deserialize)]
pub struct HookCollectRequest {
    pub project_id: String,
    pub role: String,
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub team_session_id: Option<String>,
    #[serde(default)]
    pub agent_id: Option<String>,
    #[serde(default)]
    pub hook_provider: Option<String>,
    #[serde(default)]
    pub cli: Option<String>,
    #[serde(default = "default_event_name")]
    pub hook_event_name: String,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub transcript_path: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    // hook payload 원천: hook_stdin 1차, payload 별칭 fallback (Python collect_hook 정합).
    // token 등 secret 은 mask_payload 로 저장 시 마스킹된다.
    #[serde(default)]
    pub hook_stdin: Option<serde_json::Map<String, Value>>,
    #[serde(default)]
    pub payload: Option<serde_json::Map<String, Value>>,
}

fn default_event_name() -> String {
    "Stop".to_string()
}

pub async fn collect_hook<R: WebguiRepository, P: EventPublisher, T: TranscriptPort>(
    repo: &R,
    publisher: &P,
    transcript: &T,
    req: HookCollectRequest,
) -> Result<Value, ApiError> {
    let provider = normalize_provider(req.hook_provider.as_deref().or(req.cli.as_deref()));
    let event_type = normalize_event_type(provider.as_deref().unwrap_or(""), &req.hook_event_name);
    let room_type = if req.role == "PM" { "pm" } else { "role" };
    let display_name = req.display_name.clone().unwrap_or_else(|| req.role.clone());

    let room = repo
        .upsert_room(
            &req.project_id,
            &req.role,
            &display_name,
            room_type,
            req.team_session_id.as_deref(),
            req.agent_id.as_deref(),
        )
        .await?;

    // hint payload (Python collect_hook 정합):
    //   payload = dict(hook_stdin or payload or {}) → session_id/transcript_path/cwd/agent_id setdefault.
    // hook_stdin 의 token 등 secret 은 아래 mask_payload 에서 마스킹된다.
    let mut payload: serde_json::Map<String, Value> =
        req.hook_stdin.clone().or_else(|| req.payload.clone()).unwrap_or_default();
    if let Some(v) = &req.session_id {
        payload.entry("session_id".to_string()).or_insert_with(|| v.clone().into());
    }
    if let Some(v) = &req.transcript_path {
        payload.entry("transcript_path".to_string()).or_insert_with(|| v.clone().into());
    }
    if let Some(v) = &req.cwd {
        payload.entry("cwd".to_string()).or_insert_with(|| v.clone().into());
    }
    // agent_id 는 None 이어도 키를 둔다(Python setdefault 동등).
    payload
        .entry("agent_id".to_string())
        .or_insert_with(|| match &req.agent_id {
            Some(v) => v.clone().into(),
            None => Value::Null,
        });

    let ev = repo
        .insert_runtime_event(NewEvent {
            room_id: room.room_id.clone(),
            event_type: event_type.clone(),
            source: "hook".into(),
            hook_provider: provider.clone(),
            hook_event_name: Some(req.hook_event_name.clone()),
            severity: "info".into(),
            payload_json: Some(crate::masking::mask_payload(&Value::Object(payload))),
            correlation_id: None,
            message_id: None,
            agent_session_id: None,
            occurred_at_iso: None,
        })
        .await?;

    // WS publish — hook_stop → correlation_closed (Python collect_hook→collect_event 위임 동등).
    if event_type == "hook_stop" {
        let ws = json!({
            "type": "message_update",
            "cursor": format!("{}|event:{}", ev.recorded_at, ev.event_id),
            "data": {
                "update_id": format!("event:{}", ev.event_id),
                "room_id": ev.room_id,
                "correlation_id": Value::Null,
                "update_type": "correlation_closed",
                "message": Value::Null,
                "event": { "event_type": ev.event_type, "source": ev.source, "severity": ev.severity },
                "occurred_at": ev.occurred_at,
            },
        });
        publisher.publish(&room.room_id, ws, &req.project_id);
    }

    // 세션 등록 + 증분 수집을 **모든 훅 이벤트**에서 수행한다(hook_stop 한정 아님).
    // 근거(Python 정합): collector_service.collect_event 는 session_id 가 있는 *어떤* 훅이든
    //   collector.sessions.register(...) 로 세션을 등록한다(즉시 collect 만 hook_stop).
    //   그 등록 세션을 transcript_loop 가 주기 폴링해 모든 역할의 발화를 실시간 수집한다.
    // 기존 Rust 는 hook_stop 에서만 collect→tx_sessions 등록했다. 그 결과 최근 Stop 이 없는
    //   세션(예: PM 이 뮤즈에 보낸 메시지를 받은 뮤즈 세션)은 폴링 대상에 없어 다음 Stop 까지
    //   실시간 누락됐다. SessionStart/PreToolUse/PostToolUse 등 어떤 훅이든 등록·증분수집한다.
    //   collect() 는 offset 기반이라 신규분 없으면 즉시 return(거의 무비용)이고, 등록 부작용으로
    //   1초 폴링 루프가 해당 세션을 영구 커버한다. (transcript_path 없으면 collect 가 no-op)
    {
        let hint = TranscriptHint {
            provider: provider.clone(),
            session_id: req.session_id.clone(),
            transcript_path: req.transcript_path.clone(),
            agent_id: req.agent_id.clone(),
            project_id: req.project_id.clone(),
            role: req.role.clone(),
            room_id: room.room_id.clone(),
        };
        transcript.collect(&hint).await;
    }

    // RV-55 §4: 정본 = Python 상세 응답 `{event: event_to_dict, room_id}`.
    // (Python collect_hook → collect_event → {"event": event_to_dict(ev)} + room_id)
    Ok(json!({
        "event": crate::event::event_to_dict(&ev),
        "room_id": room.room_id,
    }))
}
