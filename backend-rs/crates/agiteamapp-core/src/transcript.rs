//! transcript 즉시수집 본체: JSONL 파서 + 레코드→메시지 저장. hook_stop 트리거가 호출.
//! 레퍼런스: services/{transcript_parser,transcript_collector}.py.
//! 파서는 순수(core·테스트 가능). 파일 IO 는 TranscriptPort 구현(http)이 수행.

use serde::Deserialize;
use serde_json::{json, Value};

use crate::events::EventPublisher;
use crate::masking::{mask_text, sanitize_tool_leak};
use crate::repo::{
    compute_raw_hash, ApiError, NewMessage, RepoError, TranscriptHint, WebguiRepository,
};

pub const TRANSCRIPT_SOURCE: &str = "transcript";

/// canonical 매칭 텍스트: 공백 정규화(trim + 연속공백 1개). 터미널 래핑/공백차 흡수.
/// (Python transcript_collector.canonical_match_text 정합)
fn canonical_text(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

// --- 시스템 알림 분리 (모니터: 시스템 텍스트를 '유저 말풍선'에서 분리) -----------
// Claude harness 가 user turn 에 주입하는 시스템 알림 블록. 이런 블록'만'으로 이뤄진
// user turn 은 유저 발화가 아니라 시스템 메시지(좌측·status)로 분류한다. 실제 유저
// 텍스트가 함께 있으면 유저 발화로 보존(원본 그대로 user_message 유지).
const SYS_PAIRED_MARKERS: &[(&str, &str)] = &[
    ("<task-notification>", "</task-notification>"),
    ("<system-reminder>", "</system-reminder>"),
];
const SYS_NOTICE_MARKER: &str = "[SYSTEM NOTIFICATION - NOT USER INPUT]";

/// open..close (양끝 포함) 구간을 비탐욕적으로 모두 제거. close 가 없으면(미완 블록)
/// open 이후 전부 제거(잘린 시스템 블록도 시스템으로 취급).
fn remove_paired(s: &str, open: &str, close: &str) -> String {
    let mut out = String::new();
    let mut rest = s;
    loop {
        match rest.find(open) {
            Some(i) => {
                out.push_str(&rest[..i]);
                let after_open = &rest[i + open.len()..];
                match after_open.find(close) {
                    Some(j) => rest = &after_open[j + close.len()..],
                    None => break, // close 없음 → 이후 전부 제거
                }
            }
            None => {
                out.push_str(rest);
                break;
            }
        }
    }
    out
}

/// user turn 텍스트가 '시스템 알림 블록으로만' 구성됐는지 판별.
/// - Some(표시용텍스트): 시스템 알림 전용 turn → 시스템 메시지로 분류(래퍼 태그 토큰 제거 본문).
/// - None: 실제 유저 텍스트가 섞였거나 마커가 없음 → 일반 유저 발화로 보존.
pub fn classify_system_notification(text: &str) -> Option<String> {
    let has_marker = SYS_PAIRED_MARKERS.iter().any(|(o, _)| text.contains(o))
        || text.contains(SYS_NOTICE_MARKER);
    if !has_marker {
        return None;
    }
    // 페어 블록 제거 후 남는 실텍스트 판정.
    let mut leftover = text.to_string();
    for (open, close) in SYS_PAIRED_MARKERS {
        leftover = remove_paired(&leftover, open, close);
    }
    let trimmed = leftover.trim();
    // 남은 게 없거나, 남은 게 unpaired 시스템 공지 마커로 시작하면 '시스템 전용 turn'.
    let is_pure = trimmed.is_empty() || trimmed.starts_with(SYS_NOTICE_MARKER);
    if !is_pure {
        return None; // 실제 유저 텍스트 공존 → 유저 발화 보존
    }
    // 표시용: 래퍼 XML 태그 토큰만 제거하고 본문은 유지(FE 가 시스템 형식으로 렌더).
    let mut display = text.to_string();
    for (open, close) in SYS_PAIRED_MARKERS {
        display = display.replace(open, "").replace(close, "");
    }
    let display = display.trim().to_string();
    Some(if display.is_empty() { text.trim().to_string() } else { display })
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

        // 시스템 알림 분리: user turn 이 시스템 알림 블록으로만 구성되면 좌측 시스템 메시지로.
        //  - is_system=true → direction=inbound(좌측), message_type=status, assistant correlation/dedup 미적용.
        //  - effective_text = 래퍼 태그 제거 본문(표시·해시 기준). 일반 메시지는 원본 그대로.
        let system_display = if is_assistant {
            None
        } else {
            classify_system_notification(text)
        };
        let is_system = system_display.is_some();
        let inbound_like = is_assistant || is_system;
        let effective_text: String = system_display.unwrap_or_else(|| text.to_string());
        let text = effective_text.as_str();
        if text.is_empty() {
            continue;
        }

        let direction = if inbound_like { "inbound" } else { "outbound" };
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
        // 시스템 메시지(is_system)는 outbound 가 아니므로 이 체크 대상 아님.
        if !is_assistant && !is_system {
            let canon = canonical_text(text);
            if !canon.is_empty()
                && repo.find_outbound_text_dup(room_id, &canon).await?.is_some()
            {
                continue;
            }
        }

        let mut correlation_id: Option<String> = None;
        // assistant inbound correlation 매칭 (A안+B안, 유저 승인 2026-06-21):
        //  - A안(가시성 복원): 미매칭이어도 절대 드롭하지 않고 반드시 저장한다.
        //  - B안(correlation 정합): '직전 outbound(sent, correlation 유무 무관)' 존재를 매칭
        //    성립 기준으로 삼아 team-CLI 주도 턴(outbound.correlation_id=NULL)도 정상 inbound 로
        //    잇는다. correlation 이 있으면 이어 잇고(Some), 없으면 NULL 인 채 received 로 저장.
        //  - 방에 outbound 가 전무한 '고아' assistant 만 status='unmatched' 로 식별 보존.
        //  - 2026-06-20 드롭 룰이 막으려던 중복/노이즈는 아래 dedup(record_id ON CONFLICT /
        //    raw_hash 사전체크)이 그대로 차단하므로 재발하지 않는다.
        let mut assistant_unmatched = false;
        if is_assistant {
            match repo.find_recent_outbound(room_id).await? {
                Some(ob) => correlation_id = ob.correlation_id,
                None => assistant_unmatched = true,
            }
        }
        // 시스템 메시지: status=received(중립 inbound), message_type=status(DB CHECK 정식값).
        //   DB ck_webgui_message_type 은 'system' 을 불허 → 시스템류 정식값 'status' 채택.
        let status = if is_system {
            "received".to_string()
        } else if is_assistant {
            if assistant_unmatched { "unmatched".to_string() } else { "received".to_string() }
        } else {
            "sent".to_string()
        };
        let message_type = if is_system {
            "status".to_string()
        } else {
            rec.kind.clone()
        };
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
                attachments: None,
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
                // 시스템 메시지도 좌측(inbound) 수신 이벤트로 fanout.
                "update_type": if inbound_like { "message_received" } else { "message_sent" },
                "message": crate::message::message_to_dict(&msg, project_id, Some("websocket")),
                "event": Value::Null,
                "occurred_at": msg.occurred_at,
            },
        });
        publisher.publish(room_id, ws, project_id);
        // 운영 진단: hook 즉시 경로의 WS publish 발생 여부 확인용(느림 진단).
        // 이 로그가 보이면 hook→store→publish 정상; 안 보이면 hook 미수신/transcript_path read 실패.
        eprintln!(
            "[transcript:publish] room={room_id} project={project_id} type={} mtype={message_type_dbg} stored",
            if inbound_like { "message_received" } else { "message_sent" },
            message_type_dbg = if is_system { "status" } else { rec.kind.as_str() }
        );
        stored += 1;
    }
    Ok(stored)
}

// ── Phase 1: teamwatch(edge) 수신 계약 ───────────────────────────────────────
// edge 가 로컬 transcript 파일을 파싱(A: 포맷/위치/provider)해 정제 records 를 POST 하면,
// backend 는 room(upsert project+role)·hash·direction·correlation·dedup·mask·store·WS 를
// 전부 자기가 산출한다(B). 파일은 읽지 않는다 — provider 는 hash/저장 메타에만 쓴다.
// 핵심: 기존 store_records 를 그대로 재사용 → transcript 폴링 경로와 dedup/correlation/WS 거동 동일.

/// 수신 계약 스키마 버전(아테나 정본과 동기화). 미동의 버전은 거절하지 않고 기록만(전방호환).
pub const TRANSCRIPT_RECORDS_SCHEMA_VERSION: u32 = 1;

/// envelope.records[] 항목 — edge 가 파싱한 1건. (= TranscriptRecord 의 와이어 표현, provider 는 envelope 공통)
#[derive(Debug, Clone, Deserialize)]
pub struct TranscriptRecordIn {
    #[serde(default)]
    pub record_id: Option<String>,
    /// user_message | assistant_message
    pub kind: String,
    pub text: String,
    #[serde(default)]
    pub occurred_at: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
}

/// teamwatch(edge) → backend 수신 envelope. project+role 기반(edge 는 room_id 를 모름).
#[derive(Debug, Clone, Deserialize)]
pub struct TranscriptRecordsEnvelope {
    pub project_id: String,
    pub role: String,
    /// "claude_code" | "codex"
    pub provider: String,
    #[serde(default)]
    pub records: Vec<TranscriptRecordIn>,
    /// edge 의 파일 offset 체크포인트(정보용 — backend 는 저장만, offset 상태는 edge 소유).
    #[serde(default)]
    pub transcript_end_offset: Option<u64>,
    #[serde(default)]
    pub schema_version: Option<u32>,
}

/// edge 수신 핸들러 코어. 검증 → room upsert(project+role) → records 매핑 → store_records 위임.
pub async fn collect_transcript_records<R: WebguiRepository, P: EventPublisher>(
    repo: &R,
    publisher: &P,
    env: TranscriptRecordsEnvelope,
) -> Result<Value, ApiError> {
    // 계약 검증.
    if env.project_id.trim().is_empty() || env.role.trim().is_empty() {
        return Err(ApiError::new("invalid_envelope", 422, "project_id and role are required."));
    }
    if !matches!(env.provider.as_str(), "claude_code" | "codex") {
        return Err(ApiError::new(
            "invalid_provider",
            422,
            "provider must be claude_code or codex.",
        ));
    }
    for r in &env.records {
        if r.kind != "user_message" && r.kind != "assistant_message" {
            return Err(ApiError::new(
                "invalid_kind",
                422,
                "kind must be user_message or assistant_message.",
            ));
        }
    }

    // room 산출: (project, role) upsert. display_name 미제공 → role(collect_hook 관례 정합).
    let room_type = if env.role == "PM" { "pm" } else { "role" };
    let room = repo
        .upsert_room(&env.project_id, &env.role, &env.role, room_type, None, None)
        .await?;

    // envelope record → 내부 TranscriptRecord (provider 는 envelope 공통값).
    let records: Vec<TranscriptRecord> = env
        .records
        .iter()
        .map(|r| TranscriptRecord {
            provider: env.provider.clone(),
            record_id: r.record_id.clone(),
            kind: r.kind.clone(),
            text: r.text.clone(),
            occurred_at: r.occurred_at.clone(),
            session_id: r.session_id.clone(),
        })
        .collect();

    // 저장: 기존 store_records 재사용 — hash/direction/dedup①②/bridge-dup/correlation/mask/insert/touch/WS.
    let stored =
        store_records(repo, publisher, &env.project_id, &room.room_id, &env.role, &records).await?;

    Ok(json!({
        "accepted": true,
        "received": env.records.len(),
        "stored": stored,
        "room_id": room.room_id,
        "project_id": env.project_id,
        "role": env.role,
        "schema_version": env.schema_version.unwrap_or(TRANSCRIPT_RECORDS_SCHEMA_VERSION),
    }))
}
