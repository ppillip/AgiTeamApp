//! WG-ACT-01: read-screen poller active pulse 수신. **DB write/read 0** (인메모리 pulse + WS publish).
//! 레퍼런스: Python runtime_activity_service.collect_runtime_activity (DS-110 §6.2).

use std::collections::HashMap;
use std::sync::Mutex;

use serde::Deserialize;
use serde_json::{json, Value};

use crate::events::EventPublisher;
use crate::repo::{is_activity_role, ApiError};

pub const ACTIVITY_ACTIVE: &str = "active";
pub const REASON_READ_SCREEN: &str = "read_screen_changed";
pub const RUNTIME_ACTIVITY_EVENT: &str = "runtime_activity_changed";
pub const READ_SCREEN_POLLER_SOURCE: &str = "read_screen_poller";

#[derive(Debug, Clone, Deserialize)]
pub struct RuntimeActivityCollectRequest {
    pub project_id: String,
    #[serde(default)]
    pub team_session_id: Option<String>,
    pub role: String,
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub surface_id: Option<String>,
    pub activity: String,
    #[serde(default)]
    pub reason: Option<String>,
    pub snapshot_hash: String,
    #[serde(default)]
    pub snapshot_bytes: Option<i64>,
    #[serde(default = "default_poll_interval")]
    pub poll_interval_ms: i64,
    /// poller 관측 시각 = last_active_at (ISO 문자열).
    pub observed_at: String,
}

fn default_poll_interval() -> i64 {
    1000
}

#[derive(Debug, Clone)]
struct Pulse {
    last_active_at: String,
    last_activity_hash: String,
}

/// (project_id, role) → 최근 pulse 인메모리 저장 (영구 저장 없음 — REST 폴백이 읽음).
#[derive(Default)]
pub struct ActivityRegistry {
    map: Mutex<HashMap<(String, String), Pulse>>,
}

impl ActivityRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// REST degrade (DS-110 §9.1): pulse 있으면 ("active", last_active_at). 없으면 None.
    pub fn get_pulse(&self, project_id: &str, role: &str) -> Option<String> {
        let map = self.map.lock().unwrap();
        map.get(&(project_id.to_string(), role.to_string()))
            .map(|p| p.last_active_at.clone())
    }
}

/// active pulse 1건 처리 → registry 갱신 + WS publish. DB 미접근.
pub fn collect_runtime_activity<P: EventPublisher>(
    registry: &ActivityRegistry,
    publisher: &P,
    req: RuntimeActivityCollectRequest,
) -> Result<Value, ApiError> {
    // 1) active 외 거절
    if req.activity != ACTIVITY_ACTIVE {
        return Err(ApiError::new("invalid_activity", 422, "activity must be 'active'."));
    }
    // 2) Monitor·미지원 role 제외
    if !is_activity_role(&req.role) {
        return Err(ApiError::new(
            "invalid_role",
            422,
            "role is not an activity target (Monitor excluded).",
        ));
    }

    let observed = req.observed_at.clone();
    let key = (req.project_id.clone(), req.role.clone());

    // 3) 직전과 동일 snapshot_hash → idempotent 무시 (WS 생략)
    {
        let map = registry.map.lock().unwrap();
        if let Some(prev) = map.get(&key) {
            if prev.last_activity_hash == req.snapshot_hash {
                let last_iso = if prev.last_active_at.is_empty() {
                    observed.clone()
                } else {
                    prev.last_active_at.clone()
                };
                return Ok(json!({
                    "accepted": true,
                    "deduplicated": true,
                    "project_id": req.project_id,
                    "role": req.role,
                    "runtime_activity": ACTIVITY_ACTIVE,
                    "last_active_at": last_iso,
                    "event_id": Value::Null,
                }));
            }
        }
    }

    // 4) 인메모리 pulse 갱신
    {
        let mut map = registry.map.lock().unwrap();
        map.insert(
            key,
            Pulse {
                last_active_at: observed.clone(),
                last_activity_hash: req.snapshot_hash.clone(),
            },
        );
    }

    // 5) WS fanout — (project_id, role) 합성 topic 으로 프로젝트 전역 구독자에 push.
    //    DB event 없음 → 휘발성 합성 event_id. raw screen 미포함.
    let event_id = uuid_v4_string();
    let reason = req.reason.clone().unwrap_or_else(|| REASON_READ_SCREEN.to_string());
    let payload = json!({
        "project_id": req.project_id,
        "role": req.role,
        "surface_id": req.surface_id,
        "team_session_id": req.team_session_id,
        "runtime_activity": ACTIVITY_ACTIVE,
        "reason": reason,
        "snapshot_hash": req.snapshot_hash,
        "snapshot_bytes": req.snapshot_bytes,
        "poll_interval_ms": req.poll_interval_ms,
        "last_active_at": observed,
    });
    let ws_payload = json!({
        "type": "message_update",
        "cursor": observed,
        "data": {
            "update_id": format!("event:{event_id}"),
            "room_id": Value::Null,
            "correlation_id": Value::Null,
            "update_type": RUNTIME_ACTIVITY_EVENT,
            "message": Value::Null,
            "event": {
                "event_id": event_id,
                "event_type": RUNTIME_ACTIVITY_EVENT,
                "source": READ_SCREEN_POLLER_SOURCE,
                "severity": "info",
                "payload": payload,
                "occurred_at": observed,
            },
            "occurred_at": observed,
        },
    });
    publisher.publish(&format!("{}:{}", req.project_id, req.role), ws_payload, &req.project_id);

    Ok(json!({
        "accepted": true,
        "deduplicated": false,
        "project_id": req.project_id,
        "role": req.role,
        "runtime_activity": ACTIVITY_ACTIVE,
        "last_active_at": observed,
        "event_id": event_id,
    }))
}

/// core 는 uuid crate 미의존 — 합성 식별자만 필요하므로 경량 생성(휘발성, 충돌 무해).
fn uuid_v4_string() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static CTR: AtomicU64 = AtomicU64::new(0);
    let n = CTR.fetch_add(1, Ordering::Relaxed);
    // 휘발성 합성 식별자 (DB 저장 안 됨). Python uuid4 정합 위해 UUID 형식으로
    // (parity normalizer 가 <UUID#n> 로 흡수). act- prefix 는 정규화 미매칭 → strict FAIL 유발했음.
    format!("00000000-0000-4000-8000-{n:012x}")
}
