//! transcript 즉시수집 본체: JSONL 파서 + 레코드→메시지 저장. hook_stop 트리거가 호출.
//! 레퍼런스: services/{transcript_parser,transcript_collector}.py.
//! 파서는 순수(core·테스트 가능). 파일 IO 는 TranscriptPort 구현(http)이 수행.

use serde_json::Value;

use crate::events::EventPublisher;
use crate::masking::{mask_text, sanitize_tool_leak};
use crate::repo::{compute_raw_hash, NewMessage, RepoError, TranscriptHint, WebguiRepository};

pub const TRANSCRIPT_SOURCE: &str = "transcript";

/// canonical 매칭 텍스트: 공백 정규화(trim + 연속공백 1개). cmux 래핑/공백차 흡수.
/// (Python transcript_collector.canonical_match_text 정합)
fn canonical_text(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[derive(Debug, Clone)]
pub struct TranscriptRecord {
    pub provider: String,
    pub record_id: Option<String>,
    pub kind: String, // user_message | assistant_message
    pub text: String,
    pub occurred_at: Option<String>,
    pub session_id: Option<String>,
}

/// hook_stop → transcript 즉시수집 포트(seam). http 가 파일 읽어 store_records 호출.
pub trait TranscriptPort: Send + Sync {
    async fn collect(&self, hint: &TranscriptHint);
}

/// 비-수집 경로/테스트용 no-op.
pub struct NoopTranscript;
impl TranscriptPort for NoopTranscript {
    async fn collect(&self, _hint: &TranscriptHint) {}
}

/// message.content (str | [block]) → 표시용 본문. tool_use/reasoning 등 제외.
fn coerce_text(content: &Value) -> String {
    match content {
        Value::String(s) => s.trim().to_string(),
        Value::Array(arr) => {
            let mut parts = Vec::new();
            for block in arr {
                if let Value::String(s) = block {
                    parts.push(s.clone());
                    continue;
                }
                if let Some(obj) = block.as_object() {
                    let btype = obj.get("type").and_then(|v| v.as_str()).unwrap_or("");
                    if matches!(btype, "text" | "output_text" | "input_text") {
                        if let Some(t) = obj.get("text").and_then(|v| v.as_str()) {
                            if !t.trim().is_empty() {
                                parts.push(t.to_string());
                            }
                        }
                    }
                }
            }
            parts.iter().map(|p| p.trim()).filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n").trim().to_string()
        }
        _ => String::new(),
    }
}

fn loads_lines(text: &str) -> impl Iterator<Item = Value> + '_ {
    text.lines().filter_map(|l| {
        let l = l.trim();
        if l.is_empty() {
            return None;
        }
        serde_json::from_str::<Value>(l).ok().filter(|v| v.is_object())
    })
}

/// Claude Code transcript JSONL → records. type user/assistant 의 message.content 본문만.
pub fn parse_claude_records(text: &str) -> Vec<TranscriptRecord> {
    let mut out = Vec::new();
    for obj in loads_lines(text) {
        let rtype = obj.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if rtype != "user" && rtype != "assistant" {
            continue;
        }
        let Some(message) = obj.get("message").filter(|m| m.is_object()) else {
            continue;
        };
        let body = message.get("content").map(coerce_text).unwrap_or_default();
        if body.is_empty() {
            continue;
        }
        let kind = if rtype == "user" { "user_message" } else { "assistant_message" };
        out.push(TranscriptRecord {
            provider: "claude_code".into(),
            record_id: obj.get("uuid").and_then(|v| v.as_str()).map(|s| s.to_string()),
            kind: kind.into(),
            text: body,
            occurred_at: obj.get("timestamp").and_then(|v| v.as_str()).map(|s| s.to_string()),
            session_id: obj.get("sessionId").and_then(|v| v.as_str()).map(|s| s.to_string()),
        });
    }
    out
}

/// Codex rollout JSONL → records. response_item + payload.type=message + role user/assistant.
pub fn parse_codex_records(text: &str) -> Vec<TranscriptRecord> {
    let mut out = Vec::new();
    let mut session_id: Option<String> = None;
    for obj in loads_lines(text) {
        let ttype = obj.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let payload = obj.get("payload").cloned().unwrap_or(Value::Null);
        if ttype == "session_meta" {
            if let Some(id) = payload.get("id").and_then(|v| v.as_str()) {
                session_id = Some(id.to_string());
            }
            continue;
        }
        if ttype != "response_item" {
            continue;
        }
        if payload.get("type").and_then(|v| v.as_str()) != Some("message") {
            continue;
        }
        let role = payload.get("role").and_then(|v| v.as_str()).unwrap_or("");
        if role != "user" && role != "assistant" {
            continue;
        }
        let body = payload.get("content").map(coerce_text).unwrap_or_default();
        if body.is_empty() {
            continue;
        }
        let kind = if role == "user" { "user_message" } else { "assistant_message" };
        out.push(TranscriptRecord {
            provider: "codex".into(),
            record_id: payload.get("id").and_then(|v| v.as_str()).map(|s| s.to_string()),
            kind: kind.into(),
            text: body,
            occurred_at: obj.get("timestamp").and_then(|v| v.as_str()).map(|s| s.to_string()),
            session_id: session_id.clone(),
        });
    }
    out
}

pub fn parse_records(provider: &str, text: &str) -> Vec<TranscriptRecord> {
    match provider {
        "codex" => parse_codex_records(text),
        _ => parse_claude_records(text),
    }
}

/// records → 메시지 저장 (dedup·correlation·WS publish). 저장 건수 반환.
/// 레퍼런스: transcript_collector._store_record.
pub async fn store_records<R: WebguiRepository, P: EventPublisher>(
    repo: &R,
    publisher: &P,
    project_id: &str,
    room_id: &str,
    role: &str,
    records: &[TranscriptRecord],
) -> Result<i64, RepoError> {
    let mut stored = 0i64;
    for rec in records {
        let text = rec.text.trim();
        if text.is_empty() {
            continue;
        }
        let is_assistant = rec.kind == "assistant_message";
        let direction = if is_assistant { "inbound" } else { "outbound" };
        let raw_hash = compute_raw_hash(Some(&rec.provider), rec.record_id.as_deref(), role, text);

        // dedup (P2):
        //  - record_id 있음 → 사전 SELECT 없이 아래 create_message_on_conflict_skip 의
        //    INSERT ON CONFLICT 가 1왕복으로 처리(충돌 시 None 반환). dedup SELECT 제거.
        //  - record_id 없음 → 충돌 인덱스 대상이 아니므로 여기서 raw_hash 사전체크 유지.
        if rec.record_id.is_none()
            && repo.find_message_by_hash(room_id, TRANSCRIPT_SOURCE, &raw_hash).await?.is_some()
        {
            continue;
        }
        // bridge-dup: user(outbound) record 가 WebGUI 선저장 outbound(webgui/pm_bridge)와
        // 같은 canonical text 면 중복 → skip (Python _find_outbound_text_dup 정합).
        // WebGUI 송신(SENT) + 그 입력의 transcript 수집(LIVE) 2건 저장 방지.
        if !is_assistant {
            let canon = canonical_text(text);
            if !canon.is_empty()
                && repo.find_outbound_text_dup(room_id, &canon).await?.is_some()
            {
                continue;
            }
        }

        let mut correlation_id: Option<String> = None;
        let mut status = if is_assistant { "received" } else { "sent" }.to_string();
        let mut message_type = rec.kind.clone();
        if is_assistant {
            match repo.find_open_outbound_correlation(room_id).await? {
                Some(cid) => correlation_id = Some(cid),
                None => {
                    status = "unmatched".into();
                    message_type = "unmatched".into();
                }
            }
        }
        let occurred = match rec.occurred_at.clone() {
            Some(o) => o,
            None => repo.server_now().await?,
        };

        // P2: INSERT ON CONFLICT DO NOTHING RETURNING. 충돌(이미 저장됨)이면 None →
        // touch/publish/카운트 모두 생략(중복 WS push 방지).
        let inserted = repo
            .create_message_on_conflict_skip(NewMessage {
                room_id: room_id.to_string(),
                agent_session_id: None,
                correlation_id: correlation_id.clone(),
                role_id: role.to_string(),
                surface_id: None,
                team_session_id: None,
                direction: direction.to_string(),
                source: TRANSCRIPT_SOURCE.to_string(),
                message_type,
                provider: Some(rec.provider.clone()),
                transcript_path: None,
                transcript_offset: None,
                transcript_record_id: rec.record_id.clone(),
                // raw_text=mask_text(원본), normalized=sanitize_tool_leak(표시본). dedup(raw_hash)는 원본 기준 불변.
                raw_text: mask_text(Some(text)),
                normalized_text: sanitize_tool_leak(Some(text)).unwrap_or_default(),
                raw_hash: Some(raw_hash),
                status: status.clone(),
                occurred_at_iso: occurred,
            })
            .await?;
        let Some(msg) = inserted else {
            continue; // (provider, record_id) 중복 → 이미 저장됨, 건너뜀
        };
        repo.touch_room_last_message(room_id, &msg.message_id, &msg.occurred_at, is_assistant)
            .await?;

        let ws = serde_json::json!({
            "type": "message_update",
            "cursor": format!("{}|message:{}", msg.recorded_at, msg.message_id),
            "data": {
                "update_id": format!("message:{}", msg.message_id),
                "room_id": room_id,
                "correlation_id": correlation_id,
                "update_type": if is_assistant { "message_received" } else { "message_sent" },
                "message": crate::message::message_to_dict(&msg, project_id, Some("websocket")),
                "event": Value::Null,
                "occurred_at": msg.occurred_at,
            },
        });
        publisher.publish(room_id, ws, project_id);
        // 운영 진단: hook 즉시 경로의 WS publish 발생 여부 확인용(느림 진단).
        // 이 로그가 보이면 hook→store→publish 정상; 안 보이면 hook 미수신/transcript_path read 실패.
        eprintln!(
            "[transcript:publish] room={room_id} project={project_id} type={} stored",
            if is_assistant { "message_received" } else { "message_sent" }
        );
        stored += 1;
    }
    Ok(stored)
}
