//! 저장 포트(trait) + row/에러 타입 + 공용 헬퍼. 전송·DB 구현 무관.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// 저장 포트 에러 (DB 구현 무관 표현).
#[derive(Debug)]
pub struct RepoError(pub String);
impl std::fmt::Display for RepoError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "repo error: {}", self.0)
    }
}
impl std::error::Error for RepoError {}

/// usecase 레벨 API 에러 (Python WebguiError 정합 — code/http/message/details).
/// RV-55 §5.1: 공개 에러 envelope는 `{ok:false,error:{code,message,details}}`가 정본.
/// details 는 object 이며 없으면 `{}` 로 반환한다.
#[derive(Debug)]
pub struct ApiError {
    pub code: &'static str,
    pub http: u16,
    pub message: String,
    pub details: Value,
}
impl ApiError {
    pub fn new(code: &'static str, http: u16, message: impl Into<String>) -> Self {
        Self { code, http, message: message.into(), details: json!({}) }
    }
    /// details object 부착(예: validation errors[]). 빈 값이면 `{}` 유지.
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = details;
        self
    }
}
impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} ({}): {}", self.code, self.http, self.message)
    }
}
/// repo 실패는 500 으로 승격.
impl From<RepoError> for ApiError {
    fn from(e: RepoError) -> Self {
        ApiError::new("repo_error", 500, e.0)
    }
}

/// 방 upsert 결과.
#[derive(Debug, Clone)]
pub struct RoomRef {
    pub room_id: String,
    pub project_id: String,
    pub role_id: String,
}

/// 방 조회 row (room-scoped collect 검증용).
#[derive(Debug, Clone)]
pub struct RoomRow {
    pub room_id: String,
    pub project_id: String,
    pub role_id: String,
    pub team_session_id: Option<String>,
}

/// 메시지 row (dedup 조회 + create 반환 + 공개 응답 직렬화).
#[derive(Debug, Clone)]
pub struct MessageRow {
    pub message_id: String,
    pub room_id: String,
    pub correlation_id: Option<String>,
    pub role_id: String,
    pub surface_id: Option<String>,
    pub agent_session_id: Option<String>,
    pub team_session_id: Option<String>,
    pub direction: String,
    pub source: String,
    pub message_type: String,
    pub normalized_text: String,
    pub status: String,
    /// 이미지 첨부 공개 메타(jsonb). 없으면 `[]`. (DS-40 §4.2.1)
    pub attachments_json: Value,
    pub occurred_at: String,
    pub recorded_at: String,
    pub updated_at: String,
}

/// 메시지 생성 입력 (owned).
#[derive(Debug, Clone)]
pub struct NewMessage {
    pub room_id: String,
    pub agent_session_id: Option<String>,
    pub correlation_id: Option<String>,
    pub role_id: String,
    pub surface_id: Option<String>,
    pub team_session_id: Option<String>,
    pub direction: String,
    pub source: String,
    pub message_type: String,
    pub provider: Option<String>,
    pub transcript_path: Option<String>,
    pub transcript_offset: Option<String>,
    pub transcript_record_id: Option<String>,
    pub raw_text: Option<String>,
    pub normalized_text: String,
    // nullable (migration 0001: text, unique index는 raw_hash IS NOT NULL 조건).
    // outbound 선저장은 Python 과 동일하게 None (dedup 비대상).
    pub raw_hash: Option<String>,
    pub status: String,
    pub occurred_at_iso: String,
}

/// runtime_event 생성 입력.
#[derive(Debug, Clone)]
pub struct NewEvent {
    pub room_id: String,
    pub event_type: String,
    pub source: String,
    pub hook_provider: Option<String>,
    pub hook_event_name: Option<String>,
    pub severity: String,
    pub payload_json: Option<Value>,
    pub correlation_id: Option<String>,
    pub message_id: Option<String>,
    pub agent_session_id: Option<String>,
    pub occurred_at_iso: Option<String>,
}

/// runtime_event row (event_to_dict 응답용).
#[derive(Debug, Clone)]
pub struct EventRow {
    pub event_id: String,
    pub room_id: String,
    pub message_id: Option<String>,
    pub correlation_id: Option<String>,
    pub event_type: String,
    pub source: String,
    pub hook_provider: Option<String>,
    pub hook_event_name: Option<String>,
    pub severity: String,
    pub payload: Option<Value>,
    pub occurred_at: String,
    pub recorded_at: String,
}

/// 방 전체 컬럼 (조회 응답 room_summary_dict 용).
#[derive(Debug, Clone)]
pub struct RoomFull {
    pub room_id: String,
    pub project_id: String,
    pub role_id: String,
    pub display_name: String,
    pub agent_type: Option<String>,
    pub room_type: String,
    pub current_surface_id: Option<String>,
    pub current_agent_session_id: Option<String>,
    pub team_session_id: Option<String>,
    pub agent_id: Option<String>,
    pub ready_state: String,
    pub last_message_id: Option<String>,
    pub last_message_at: Option<String>,
    pub read_marker_at: Option<String>,
    pub unread_count: i64,
}

/// 프로젝트 집계 (distinct_projects_with_rooms).
#[derive(Debug, Clone)]
pub struct ProjectAgg {
    pub project_id: String,
    pub room_count: i64,
    pub roles: Vec<String>,
    pub last_message_at: Option<String>,
}

/// 메시지 페이지 조회 파라미터.
#[derive(Debug, Clone, Default)]
pub struct MessagePage {
    pub limit: i64,
    pub direction: String, // asc | desc
    /// keyset 커서 (occurred_at_text, message_id). desc=before, asc=after.
    pub cursor: Option<(String, String)>,
    pub correlation_id: Option<String>,
}

/// transcript 즉시수집 hint (hook_stop 트리거 시 사용).
#[derive(Debug, Clone)]
pub struct TranscriptHint {
    pub provider: Option<String>,
    pub session_id: Option<String>,
    pub transcript_path: Option<String>,
    pub agent_id: Option<String>,
    pub project_id: String,
    pub role: String,
    pub room_id: String,
}

/// 전송·DB 무관 저장 포트. agiteamapp-db(sqlx)가 구현한다.
pub trait WebguiRepository: Send + Sync {
    async fn upsert_room(
        &self,
        project_id: &str,
        role_id: &str,
        display_name: &str,
        room_type: &str,
        team_session_id: Option<&str>,
        agent_id: Option<&str>,
    ) -> Result<RoomRef, RepoError>;

    async fn get_room(&self, room_id: &str) -> Result<Option<RoomRow>, RepoError>;

    async fn insert_runtime_event(&self, ev: NewEvent) -> Result<EventRow, RepoError>;

    async fn find_message_by_record(
        &self,
        provider: &str,
        record_id: &str,
    ) -> Result<Option<MessageRow>, RepoError>;

    async fn find_message_by_hash(
        &self,
        room_id: &str,
        source: &str,
        raw_hash: &str,
    ) -> Result<Option<MessageRow>, RepoError>;

    /// 방의 가장 최근 미닫힘 outbound 의 correlation_id.
    async fn find_open_outbound_correlation(
        &self,
        room_id: &str,
    ) -> Result<Option<String>, RepoError>;

    async fn create_message(&self, m: NewMessage) -> Result<MessageRow, RepoError>;

    /// transcript 저장 전용(P2): `(provider, transcript_record_id)` 유니크 인덱스에
    /// `INSERT ... ON CONFLICT DO NOTHING RETURNING` 으로 dedup SELECT + INSERT 2왕복을
    /// **1왕복**으로 축소한다. 충돌(이미 저장됨)이면 `None` 반환 → 호출자는 publish 생략.
    /// transcript_record_id 가 None 인 record 는 충돌 대상이 아니므로 항상 신규 INSERT 된다
    /// (그 경우의 dedup 은 호출자가 raw_hash 사전체크로 처리).
    async fn create_message_on_conflict_skip(
        &self,
        m: NewMessage,
    ) -> Result<Option<MessageRow>, RepoError>;

    async fn touch_room_last_message(
        &self,
        room_id: &str,
        message_id: &str,
        occurred_at_iso: &str,
        inbound: bool,
    ) -> Result<(), RepoError>;

    // --- 조회(GET) ---------------------------------------------------------
    async fn get_room_full(&self, room_id: &str) -> Result<Option<RoomFull>, RepoError>;

    /// 방의 active(ended_at IS NULL) agent_session collector_state. 없으면 None.
    /// (Python rooms.py active_session_for_room().collector_state 정합)
    async fn active_collector_state(&self, room_id: &str) -> Result<Option<String>, RepoError>;

    /// canonical(공백 정규화) text 가 일치하는 최근 bridge outbound(webgui/pm_bridge)를 반환.
    /// transcript 의 user(outbound) record 와 WebGUI 선저장본의 중복 저장 방지.
    /// (Python transcript_collector._find_outbound_text_dup 정합)
    async fn find_outbound_text_dup(
        &self,
        room_id: &str,
        canonical_text: &str,
    ) -> Result<Option<MessageRow>, RepoError>;

    /// 프로젝트의 방 목록 (last_message_at desc nullslast, created_at asc).
    async fn list_rooms(&self, project_id: &str) -> Result<Vec<RoomFull>, RepoError>;

    /// 단건 메시지 조회 (last_message 채움용).
    async fn get_message(&self, message_id: &str) -> Result<Option<MessageRow>, RepoError>;

    /// 방 메시지 페이지 (limit+1 반환 → 호출자가 has_more 판정).
    async fn list_room_messages(
        &self,
        room_id: &str,
        page: &MessagePage,
    ) -> Result<Vec<MessageRow>, RepoError>;

    /// 방 보유 프로젝트 집계.
    async fn list_projects(&self) -> Result<Vec<ProjectAgg>, RepoError>;

    /// 방 runtime_event 목록 (occurred_at desc, limit+1).
    async fn list_events(
        &self,
        room_id: &str,
        limit: i64,
        correlation_id: Option<&str>,
    ) -> Result<Vec<EventRow>, RepoError>;

    /// polling fallback: recorded_at > after 인 메시지 (asc, limit).
    async fn updates_since(
        &self,
        room_id: &str,
        after: Option<&str>,
        limit: i64,
    ) -> Result<Vec<MessageRow>, RepoError>;

    /// 읽음 표시. read_marker_at = COALESCE(read_until, last_message_at, now()), unread=0.
    /// 반환: (read_marker_at, unread_count, updated_at).
    async fn mark_read(
        &self,
        room_id: &str,
        read_until: Option<&str>,
    ) -> Result<(Option<String>, i64, String), RepoError>;

    /// DB 서버 시각 (now()::text). server_time 응답용.
    async fn server_now(&self) -> Result<String, RepoError>;

    /// 메시지 status 갱신 (pending→sent/failed). updated_at=now().
    async fn set_message_status(&self, message_id: &str, status: &str) -> Result<(), RepoError>;
}

// --- 공용 헬퍼 (Python hook_normalizer / log_collector 정합) ---------------

/// PM 포함 7역할만 활동 관측 대상 (DS-110 §12.1). Monitor·미지원 role 제외.
pub fn is_activity_role(role: &str) -> bool {
    matches!(
        role,
        "PM" | "Architect" | "DeveloperBE" | "DeveloperFE" | "Designer" | "QA" | "DevOps"
    )
}

/// provider+event_name → 공통 event_type (Python normalize_event_type 정합).
pub fn normalize_event_type(_provider: &str, event_name: &str) -> String {
    let lname = event_name.trim().to_lowercase();
    const NAME_KEYWORD_MAP: &[(&str, &str)] = &[
        ("userpromptsubmit", "hook_user_prompt"),
        ("pretool", "hook_pre_tool_use"),
        ("posttool", "hook_post_tool_use"),
        ("tool.execute.before", "hook_pre_tool_use"),
        ("tool.execute.after", "hook_post_tool_use"),
        ("stop", "hook_stop"),
        ("session", "hook_session"),
        ("invocation", "hook_session"),
    ];
    for (needle, etype) in NAME_KEYWORD_MAP {
        if lname.contains(needle) {
            return etype.to_string();
        }
    }
    "hook_session".to_string()
}

/// provider 정규화 (claude/claude-code → claude_code).
pub fn normalize_provider(p: Option<&str>) -> Option<String> {
    let v = p?.trim().to_lowercase();
    match v.as_str() {
        "claude" | "claude_code" | "claude-code" => Some("claude_code".to_string()),
        "" => None,
        other => Some(other.to_string()),
    }
}

/// dedup raw_hash 계산 (Python: "sha256:" + sha256("provider|record|role|text")).
pub fn compute_raw_hash(
    provider: Option<&str>,
    record_id: Option<&str>,
    role_id: &str,
    normalized_text: &str,
) -> String {
    let material = format!(
        "{}|{}|{}|{}",
        provider.unwrap_or(""),
        record_id.unwrap_or(""),
        role_id,
        normalized_text
    );
    let digest = Sha256::digest(material.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("sha256:{hex}")
}
