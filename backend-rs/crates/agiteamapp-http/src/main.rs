//! agiteamapp-http — axum adapter. 라우팅·핸들러는 core usecase 를 호출만 한다.
//! DB 구현은 agiteamapp-db(PgRepository), WS fanout 은 broadcast 기반 WsHub 로 주입.

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use agiteamapp_core::{
    collect_event, collect_hook, collect_message, collect_runtime_activity, get_message,
    list_events_uc, list_projects, list_room_messages, list_rooms, mark_read,
    message_to_dict, message_update_type, message_updates,
    runtime_status, send_message, ActivityRegistry, ApiError, ArtifactChangeBuffer,
    CollectEventRequest, CollectMessageRequest, DiscoveryRegistry, EventPublisher,
    HookCollectRequest, RuntimeActivityCollectRequest,
    parse_records, store_records, ArtifactService, SendMessageRequest, TranscriptHint,
    TranscriptPort, WebguiRepository,
};
use agiteamapp_db::PgRepository;
use agiteamapp_mux::{CmuxAdapter, DummyMux, MuxAdapter};
use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::header::CONTENT_TYPE;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{routing::get, routing::post, Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::broadcast;

// --- WS hub (broadcast 기반 EventPublisher 구현) ----------------------------

#[derive(Clone)]
struct WsEvent {
    topic: String,
    payload: Value,
    project_id: String,
}

struct WsHub {
    tx: broadcast::Sender<WsEvent>,
}
impl WsHub {
    fn new() -> Self {
        let (tx, _) = broadcast::channel(1024);
        Self { tx }
    }
    fn subscribe(&self) -> broadcast::Receiver<WsEvent> {
        self.tx.subscribe()
    }
}
impl EventPublisher for WsHub {
    fn publish(&self, topic: &str, payload: Value, project_id: &str) {
        // 구독자 없으면 send 는 Err → 무시(정상).
        let _ = self.tx.send(WsEvent {
            topic: topic.to_string(),
            payload,
            project_id: project_id.to_string(),
        });
    }
}

/// transcript 즉시수집: hint.transcript_path 파일을 **직전 offset 이후 신규 바이트만** tail 해
/// 파싱→store. (Python transcript_collector._read_new 의 offset+seek 방식 이식, P1)
/// 핵심: 매 훅마다 파일 전체를 read_to_string 하고 전체 record 를 재dedup 하던 O(N²) 거동을 제거,
/// 신규분만 처리(O(신규))한다. offset 상태는 AppState.tx_offsets 에 파일경로별로 공유 보존한다.
struct TranscriptCollector {
    repo: Arc<PgRepository>,
    hub: Arc<WsHub>,
    /// transcript_path → 직전까지 읽은 바이트 offset (요청 간 공유 보존).
    offsets: Arc<Mutex<HashMap<String, u64>>>,
    /// (project_id, role_id) → 그 surface 의 최신 세션 hint. 폴링 fallback 루프가 이 목록을
    /// 주기적으로 재수집해, hook 레이스/idle 로 정체된 발화를 다음 훅을 기다리지 않고 flush 한다.
    /// role 키로 두어 7 surface(6역할+PM) 각각 정확히 1개의 '현재 세션'만 폴링한다(세션 resume
    /// 시 최신 transcript_path 로 자동 갱신, stale 누적 없음). (Python session_registry 등가)
    sessions: Arc<Mutex<HashMap<(String, String), TranscriptHint>>>,
    /// transcript_path 별 collect 직렬화 락. poll 루프와 hook 의 동시 collect 가 같은 offset 을
    /// 읽어 같은 record 를 중복 insert 하는 경합을 차단한다(특히 record_id 없는 codex: dedup 이
    /// SELECT(find_message_by_hash) 기반이라 미커밋 동시 insert 를 못 보고 둘 다 insert 됨).
    path_locks: Arc<Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>>,
}
impl TranscriptPort for TranscriptCollector {
    async fn collect(&self, hint: &TranscriptHint) {
        use tokio::io::{AsyncReadExt, AsyncSeekExt};

        let Some(path) = &hint.transcript_path else {
            return;
        };
        // 같은 파일에 대한 collect 를 직렬화(동시 collect → 같은 offset 재읽기 → 중복 insert 방지).
        let path_lock = {
            let mut m = self.path_locks.lock().unwrap();
            m.entry(path.clone())
                .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(())))
                .clone()
        };
        let _serialize = path_lock.lock().await;
        // 폴링 fallback 이 이 surface 를 알도록 (project, role) 키로 등록(최신 hint 로 갱신).
        // path 는 아래 offset 키로 계속 쓰인다. idle 시에도 루프가 [offset..EOF] 를 확인해
        // hook 레이스로 밀린 신규 record 를 즉시 flush 한다.
        {
            let mut s = self.sessions.lock().unwrap();
            s.insert((hint.project_id.clone(), hint.role.clone()), hint.clone());
        }
        let mut file = match tokio::fs::File::open(path).await {
            Ok(f) => f,
            Err(e) => {
                eprintln!("[transcript] open 실패(무시) {path}: {e}");
                return;
            }
        };
        let size = match file.metadata().await {
            Ok(m) => m.len(),
            Err(e) => {
                eprintln!("[transcript] stat 실패(무시) {path}: {e}");
                return;
            }
        };
        // 직전 offset 조회. rotation/truncate(파일이 줄어듦)면 0 으로 리셋(Python 정합).
        // std Mutex 는 .await 이전에 즉시 해제(가드를 await 너머로 들고 가지 않음).
        let mut offset = {
            let map = self.offsets.lock().unwrap();
            map.get(path).copied().unwrap_or(0)
        };
        if size < offset {
            offset = 0;
        }
        if size <= offset {
            // 신규 바이트 없음 → 파일 파싱·DB dedup 모두 스킵(O(0)). 과거 전체 재읽기 제거.
            return;
        }
        if offset > 0 {
            if let Err(e) = file.seek(std::io::SeekFrom::Start(offset)).await {
                eprintln!("[transcript] seek 실패(무시) {path}: {e}");
                return;
            }
        }
        let mut buf = Vec::new();
        if let Err(e) = file.read_to_end(&mut buf).await {
            eprintln!("[transcript] read 실패(무시) {path}: {e}");
            return;
        }
        // 부분 라인(쓰기 진행 중) 유실 방지: 마지막 개행까지만 처리, offset 도 거기까지만 전진.
        let end = match buf.iter().rposition(|&b| b == b'\n') {
            Some(i) => i + 1,
            None => return, // 아직 완결된 라인 없음 → 다음 훅 대기
        };
        let new_offset = offset + end as u64;
        let text = String::from_utf8_lossy(&buf[..end]);
        let provider = hint.provider.as_deref().unwrap_or("claude_code");
        let records = parse_records(provider, &text);
        match store_records(
            self.repo.as_ref(),
            self.hub.as_ref(),
            &hint.project_id,
            &hint.room_id,
            &hint.role,
            &records,
        )
        .await
        {
            Ok(n) => {
                // offset 전진은 저장 성공 시에만(실패 시 다음 훅이 같은 구간 재시도).
                self.offsets.lock().unwrap().insert(path.clone(), new_offset);
                eprintln!("[transcript] room={} stored={n} offset={new_offset}", hint.room_id);
            }
            Err(e) => eprintln!("[transcript] store 실패(무시): {e}"),
        }
    }
}

#[derive(Clone)]
struct AppState {
    repo: Arc<PgRepository>,
    activity: Arc<ActivityRegistry>,
    hub: Arc<WsHub>,
    mux: Arc<MuxAdapter>,
    discovery: Arc<DiscoveryRegistry>,
    changes: Arc<ArtifactChangeBuffer>,
    selected_project: Option<String>,
    projects_base: String,
    api_token: Option<String>,
    collector_token: Option<String>,
    // WG-ART-04: watcher 비활성이면 changes 폴링은 503(Python artifact_watcher_unavailable 정합).
    artifact_watcher_enabled: bool,
    // P1: transcript 증분 tail offset 공유 상태(파일경로별). 요청마다 컬렉터를 새로 만들어도
    // 이 Arc 를 공유하므로 offset 이 보존된다(Python session_registry_singleton 역할).
    tx_offsets: Arc<Mutex<HashMap<String, u64>>>,
    // 폴링 fallback 용 세션 레지스트리((project, role) → 최신 hint). 모든 훅에서 등록되고
    // 백그라운드 transcript_poll_loop 가 주기적으로 재수집한다(Python transcript_loop 등가).
    tx_sessions: Arc<Mutex<HashMap<(String, String), TranscriptHint>>>,
    // transcript_path 별 collect 직렬화 락(중복 insert 경합 차단).
    tx_path_locks: Arc<Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>>,
}

impl AppState {
    fn transcript(&self) -> TranscriptCollector {
        TranscriptCollector {
            repo: self.repo.clone(),
            hub: self.hub.clone(),
            offsets: self.tx_offsets.clone(),
            sessions: self.tx_sessions.clone(),
            path_locks: self.tx_path_locks.clone(),
        }
    }
}

impl AppState {
    /// project_id + root_type → ArtifactService (루트 = <base>/<pid>/<subdir>).
    fn artifact_svc(&self, project_id: Option<&str>, root_type: Option<&str>) -> ArtifactService {
        let pid = project_id
            .map(|s| s.to_string())
            .or_else(|| self.selected_project.clone())
            .unwrap_or_else(|| "AgiTeamApp".to_string());
        let subdir = match root_type.unwrap_or("").to_lowercase().as_str() {
            "system" => "system",
            "persona" => "brain",
            _ => "documents",
        };
        let root = std::path::PathBuf::from(format!(
            "{}/{}/{}",
            self.projects_base.trim_end_matches('/'),
            pid,
            subdir
        ));
        ArtifactService::new(root, format!("{subdir}/"))
    }
}

// --- 공통 응답 헬퍼 ----------------------------------------------------------

fn err_response(e: ApiError) -> (StatusCode, Json<Value>) {
    let status = StatusCode::from_u16(e.http).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (
        status,
        Json(json!({
            "ok": false,
            "error": { "code": e.code, "message": e.message, "details": e.details },
        })),
    )
}

// --- 에러 표준 envelope 미들웨어 (RV-55 §5.2) --------------------------------
// axum 기본 extractor(Query/Json/Path) reject 는 평문/빈 본문 4xx 를 낸다.
// 응답 후처리로 이를 정본 `{ok:false,error:{code,message,details}}` envelope 로 변환한다.
// usecase 오류(err_response, application/json)는 이미 envelope 이므로 건드리지 않는다.
// (커스텀 extractor trait impl 은 axum 0.7.9 시그니처 이슈로 회피 — 단일 지점 후처리)
async fn envelope_errors_mw(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    let resp = next.run(req).await;
    let status = resp.status();
    if !(status.is_client_error() || status.is_server_error()) {
        return resp;
    }
    // 이미 JSON 응답(usecase envelope)이면 그대로 둔다.
    let is_json = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.contains("application/json"))
        .unwrap_or(false);
    if is_json {
        return resp;
    }
    // 평문/빈 본문 오류 → envelope 변환. error.code 는 status 로 역추론.
    let (parts, body) = resp.into_parts();
    let bytes = axum::body::to_bytes(body, 64 * 1024).await.unwrap_or_default();
    let text = String::from_utf8_lossy(&bytes).trim().to_string();
    let code = match status {
        StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY => "invalid_request",
        StatusCode::NOT_FOUND => "not_found",
        StatusCode::METHOD_NOT_ALLOWED => "method_not_allowed",
        StatusCode::UNAUTHORIZED => "unauthorized",
        _ => "error",
    };
    let message = if text.is_empty() { "Request failed.".to_string() } else { text };
    (
        parts.status,
        Json(json!({
            "ok": false,
            "error": { "code": code, "message": message, "details": {} },
        })),
    )
        .into_response()
}

// --- META (DS-40, Python 정합) -----------------------------------------------

const META_VERSION: &str = "0.1.0";

async fn healthz(State(s): State<AppState>) -> (StatusCode, Json<Value>) {
    let project_id = s.selected_project.clone().unwrap_or_else(|| "Panthea".to_string());
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "data": { "status": "ok", "version": META_VERSION, "project_id": project_id },
        })),
    )
}

async fn root_meta() -> (StatusCode, Json<Value>) {
    (
        StatusCode::OK,
        Json(json!({
            "ok": true,
            "data": { "service": "agiteamapp-webgui-backend", "docs": "/docs" },
        })),
    )
}
fn ok_201(data: Value) -> (StatusCode, Json<Value>) {
    (StatusCode::CREATED, Json(json!({ "ok": true, "data": data })))
}
fn ok_200(data: Value) -> (StatusCode, Json<Value>) {
    (StatusCode::OK, Json(json!({ "ok": true, "data": data })))
}

// --- 수집 입구 (POST) --------------------------------------------------------

async fn hook_collect(
    State(s): State<AppState>,
    Json(req): Json<HookCollectRequest>,
) -> (StatusCode, Json<Value>) {
    let tr = s.transcript();
    match collect_hook(s.repo.as_ref(), s.hub.as_ref(), &tr, req).await {
        Ok(r) => ok_201(r),
        Err(e) => err_response(e),
    }
}

async fn runtime_activity_collect(
    State(s): State<AppState>,
    Json(req): Json<RuntimeActivityCollectRequest>,
) -> (StatusCode, Json<Value>) {
    match collect_runtime_activity(s.activity.as_ref(), s.hub.as_ref(), req) {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

async fn message_collect(
    State(s): State<AppState>,
    Path(room_id): Path<String>,
    Json(req): Json<CollectMessageRequest>,
) -> (StatusCode, Json<Value>) {
    match collect_message(s.repo.as_ref(), s.hub.as_ref(), &room_id, req).await {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

async fn event_collect(
    State(s): State<AppState>,
    Path(room_id): Path<String>,
    Json(req): Json<CollectEventRequest>,
) -> (StatusCode, Json<Value>) {
    let tr = s.transcript();
    match collect_event(s.repo.as_ref(), s.hub.as_ref(), &tr, &room_id, req).await {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

async fn message_send(
    State(s): State<AppState>,
    Json(req): Json<SendMessageRequest>,
) -> (StatusCode, Json<Value>) {
    let pid = req
        .project_id
        .clone()
        .or_else(|| s.selected_project.clone())
        .unwrap_or_else(|| "AgiTeamApp".to_string());
    let correlation_id = uuid::Uuid::new_v4().to_string();
    match send_message(
        s.repo.as_ref(),
        s.mux.as_ref(),
        s.hub.as_ref(),
        &pid,
        &correlation_id,
        req,
    )
    .await
    {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

// --- 조회 (GET) --------------------------------------------------------------

async fn projects_list(State(s): State<AppState>) -> (StatusCode, Json<Value>) {
    match list_projects(s.repo.as_ref(), s.discovery.as_ref(), s.selected_project.as_deref(), &s.projects_base).await {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct RoomsQuery {
    project_id: Option<String>,
    role_id: Option<String>,
    #[serde(default = "default_true")]
    with_last_message: bool,
}
fn default_true() -> bool {
    true
}

async fn rooms_list(
    State(s): State<AppState>,
    Query(q): Query<RoomsQuery>,
) -> (StatusCode, Json<Value>) {
    let pid = q
        .project_id
        .or_else(|| s.selected_project.clone())
        .unwrap_or_else(|| "AgiTeamApp".to_string());
    match list_rooms(
        s.repo.as_ref(),
        s.activity.as_ref(),
        s.discovery.as_ref(),
        &pid,
        q.role_id.as_deref(),
        q.with_last_message,
    )
    .await
    {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct MessagesQuery {
    #[serde(default = "default_limit")]
    limit: i64,
    #[serde(default = "default_direction")]
    direction: String,
    cursor: Option<String>,
}
fn default_limit() -> i64 {
    20
}
fn default_direction() -> String {
    "desc".to_string()
}

async fn room_messages(
    State(s): State<AppState>,
    Path(room_id): Path<String>,
    Query(q): Query<MessagesQuery>,
) -> (StatusCode, Json<Value>) {
    let limit = q.limit.clamp(1, 200);
    match list_room_messages(s.repo.as_ref(), s.activity.as_ref(), s.discovery.as_ref(), &room_id, limit, &q.direction, q.cursor)
        .await
    {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

async fn message_get(
    State(s): State<AppState>,
    Path(message_id): Path<String>,
) -> (StatusCode, Json<Value>) {
    match get_message(s.repo.as_ref(), &message_id).await {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct UpdatesQuery {
    room_id: String,
    project_id: String,
    after: Option<String>,
    #[serde(default = "default_updates_limit")]
    limit: i64,
}
fn default_updates_limit() -> i64 {
    50
}

async fn message_updates_get(
    State(s): State<AppState>,
    Query(q): Query<UpdatesQuery>,
) -> (StatusCode, Json<Value>) {
    let limit = q.limit.clamp(1, 200);
    match message_updates(s.repo.as_ref(), &q.room_id, &q.project_id, q.after, limit).await {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct RuntimeQuery {
    project_id: Option<String>,
    room_id: Option<String>,
    role_id: Option<String>,
}

async fn runtime_status_get(
    State(s): State<AppState>,
    Query(q): Query<RuntimeQuery>,
) -> (StatusCode, Json<Value>) {
    let pid = q
        .project_id
        .or_else(|| s.selected_project.clone())
        .unwrap_or_else(|| "AgiTeamApp".to_string());
    match runtime_status(s.repo.as_ref(), s.discovery.as_ref(), &pid, q.room_id.as_deref(), q.role_id.as_deref()).await {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct EventsQuery {
    #[serde(default = "default_events_limit")]
    limit: i64,
    correlation_id: Option<String>,
}
fn default_events_limit() -> i64 {
    50
}

async fn room_events(
    State(s): State<AppState>,
    Path(room_id): Path<String>,
    Query(q): Query<EventsQuery>,
) -> (StatusCode, Json<Value>) {
    let limit = q.limit.clamp(1, 200);
    match list_events_uc(s.repo.as_ref(), &room_id, limit, q.correlation_id.as_deref()).await {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct ReadRequest {
    read_until: Option<String>,
    last_read_message_id: Option<String>,
}

async fn room_mark_read(
    State(s): State<AppState>,
    Path(room_id): Path<String>,
    Json(req): Json<ReadRequest>,
) -> (StatusCode, Json<Value>) {
    match mark_read(
        s.repo.as_ref(),
        &room_id,
        req.read_until.as_deref(),
        req.last_read_message_id.as_deref(),
    )
    .await
    {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

// --- artifacts (WG-ART) ------------------------------------------------------

#[derive(Debug, Deserialize)]
struct TreeQuery {
    project_id: Option<String>,
    root_type: Option<String>,
    path: Option<String>,
    #[serde(default = "default_depth")]
    depth: i64,
    #[serde(default)]
    recursive: bool,
    #[serde(default = "default_true")]
    include_files: bool,
    #[serde(default)]
    include_hidden: bool,
    extensions: Option<String>,
}
fn default_depth() -> i64 {
    1
}

#[derive(Debug, Deserialize)]
struct ChangesQuery {
    project_id: String,
    after: Option<String>,
    #[serde(default = "default_changes_limit")]
    limit: usize,
}
fn default_changes_limit() -> usize {
    100
}

async fn artifacts_changes(
    State(s): State<AppState>,
    Query(q): Query<ChangesQuery>,
) -> (StatusCode, Json<Value>) {
    // WG-ART-04: watcher 비활성 환경에서는 Python 과 동일하게 503 (polling fallback 없음).
    if !s.artifact_watcher_enabled {
        return err_response(ApiError::new(
            "artifact_watcher_unavailable",
            503,
            "Artifact watcher is not active.",
        ));
    }
    let limit = q.limit.clamp(1, 500);
    let (updates, next_cursor) = s.changes.changes_after(&q.project_id, q.after.as_deref(), limit);
    ok_200(json!({ "updates": updates, "next_cursor": next_cursor }))
}

async fn artifacts_tree(
    State(s): State<AppState>,
    Query(q): Query<TreeQuery>,
) -> (StatusCode, Json<Value>) {
    let svc = s.artifact_svc(q.project_id.as_deref(), q.root_type.as_deref());
    let exts = q.extensions.as_ref().map(|e| {
        e.split(',').filter(|x| !x.trim().is_empty()).map(|x| x.trim().to_lowercase()).collect::<Vec<_>>()
    });
    match svc.list_tree(q.path.as_deref(), q.depth, q.recursive, q.include_files, q.include_hidden, exts, 2000, 6) {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct FileQuery {
    path: String,
    project_id: Option<String>,
    root_type: Option<String>,
    #[serde(default = "default_true")]
    sanitize: bool,
}

async fn artifacts_file(
    State(s): State<AppState>,
    Query(q): Query<FileQuery>,
) -> (StatusCode, Json<Value>) {
    let svc = s.artifact_svc(q.project_id.as_deref(), q.root_type.as_deref());
    match svc.read_file(&q.path, q.sanitize, 1_048_576, q.root_type.as_deref()) {
        Ok((data, status)) => (StatusCode::from_u16(status).unwrap_or(StatusCode::OK), Json(json!({ "ok": true, "data": data }))),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct WriteQuery {
    project_id: Option<String>,
    root_type: Option<String>,
}
#[derive(Debug, Deserialize)]
struct WriteBody {
    path: String,
    content: String,
    project_id: Option<String>,
    root_type: Option<String>,
}

async fn artifacts_write(
    State(s): State<AppState>,
    Query(q): Query<WriteQuery>,
    Json(body): Json<WriteBody>,
) -> (StatusCode, Json<Value>) {
    let pid = body.project_id.or(q.project_id);
    let rt = body.root_type.or(q.root_type);
    let svc = s.artifact_svc(pid.as_deref(), rt.as_deref());
    match svc.write_file(&body.path, &body.content) {
        Ok(data) => ok_200(data),
        Err(e) => err_response(e),
    }
}

#[derive(Debug, Deserialize)]
struct StreamQuery {
    path: String,
    project_id: Option<String>,
    root_type: Option<String>,
    #[serde(default)]
    variant: Option<String>,
}

async fn artifacts_stream(
    State(s): State<AppState>,
    headers: axum::http::HeaderMap,
    Query(q): Query<StreamQuery>,
) -> Response {
    if q.variant.as_deref() == Some("preview") {
        return err_response(ApiError::new("render_pending", 202, "Conversion preview is not ready.")).into_response();
    }
    let svc = s.artifact_svc(q.project_id.as_deref(), q.root_type.as_deref());
    let (abs, mime, size) = match svc.open_stream(&q.path) {
        Ok(v) => v,
        Err(e) => return err_response(e).into_response(),
    };
    let bytes = match std::fs::read(&abs) {
        Ok(b) => b,
        Err(_) => return err_response(ApiError::new("artifact_path_not_found", 404, "not found")).into_response(),
    };
    let (mut start, mut end, mut status) = (0usize, size as usize - 1, StatusCode::OK);
    let mut resp_headers = vec![
        ("Accept-Ranges".to_string(), "bytes".to_string()),
        ("Content-Type".to_string(), mime.clone()),
        ("X-Content-Type-Options".to_string(), "nosniff".to_string()),
    ];
    if mime == "text/html" || mime == "image/svg+xml" {
        resp_headers.push((
            "Content-Security-Policy".to_string(),
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:".to_string(),
        ));
    }
    if let Some(rh) = headers.get("range").and_then(|v| v.to_str().ok()) {
        if let Some(spec) = rh.strip_prefix("bytes=") {
            let part = spec.split(',').next().unwrap_or("");
            let (s_str, e_str) = part.split_once('-').unwrap_or(("", ""));
            let s_val = s_str.trim().parse::<usize>().unwrap_or(0);
            let e_val = e_str.trim().parse::<usize>().unwrap_or(size as usize - 1).min(size as usize - 1);
            if s_val > e_val || s_val >= size as usize {
                return Response::builder()
                    .status(416)
                    .header("Content-Range", format!("bytes */{size}"))
                    .body(axum::body::Body::empty())
                    .unwrap();
            }
            start = s_val;
            end = e_val;
            status = StatusCode::PARTIAL_CONTENT;
            resp_headers.push(("Content-Range".to_string(), format!("bytes {start}-{end}/{size}")));
        }
    }
    let slice = bytes[start..=end].to_vec();
    resp_headers.push(("Content-Length".to_string(), slice.len().to_string()));
    let mut builder = Response::builder().status(status);
    for (k, v) in resp_headers {
        builder = builder.header(k, v);
    }
    builder.body(axum::body::Body::from(slice)).unwrap()
}

// --- attachments (WG-MSG-06) -------------------------------------------------

fn project_root(base: &str, project_id: &str) -> std::path::PathBuf {
    std::path::PathBuf::from(format!("{}/{}", base.trim_end_matches('/'), project_id))
}
fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

async fn attachment_upload(
    State(s): State<AppState>,
    mut mp: axum::extract::Multipart,
) -> (StatusCode, Json<Value>) {
    let mut project_id: Option<String> = None;
    let mut client_attachment_id: Option<String> = None;
    let mut filename: Option<String> = None;
    let mut data: Vec<u8> = Vec::new();
    while let Ok(Some(field)) = mp.next_field().await {
        match field.name() {
            Some("project_id") => project_id = field.text().await.ok(),
            Some("client_attachment_id") => client_attachment_id = field.text().await.ok(),
            Some("file") => {
                filename = field.file_name().map(|s| s.to_string());
                if let Ok(b) = field.bytes().await {
                    data = b.to_vec();
                }
            }
            _ => {}
        }
    }
    let pid = project_id
        .or_else(|| s.selected_project.clone())
        .unwrap_or_else(|| "AgiTeamApp".to_string());
    let svc = agiteamapp_core::AttachmentService::new(&project_root(&s.projects_base, &pid));
    let attachment_id = format!("att_{}", uuid::Uuid::new_v4().simple());
    let sha_hex = {
        use sha2::{Digest, Sha256};
        let d = Sha256::digest(&data);
        d.iter().map(|b| format!("{b:02x}")).collect::<String>()
    };
    match svc.save(
        &pid,
        &data,
        filename.as_deref(),
        client_attachment_id.as_deref(),
        &attachment_id,
        now_epoch(),
        24 * 3600,
        10 * 1024 * 1024,
        &sha_hex,
    ) {
        Ok(att) => ok_201(json!({ "attachment": att })),
        Err(e) => err_response(e),
    }
}

async fn attachment_preview(
    State(s): State<AppState>,
    Path(attachment_id): Path<String>,
) -> Response {
    // self-contained: projects_base 하위 프로젝트들을 스캔해 소유 project 해소.
    let now = now_epoch();
    let entries = std::fs::read_dir(&s.projects_base).ok();
    if let Some(rd) = entries {
        for e in rd.flatten() {
            let pid = e.file_name().to_string_lossy().to_string();
            let svc = agiteamapp_core::AttachmentService::new(&project_root(&s.projects_base, &pid));
            if let Ok((abs, mime)) = svc.resolve(&pid, &attachment_id, now) {
                let bytes = match std::fs::read(&abs) {
                    Ok(b) => b,
                    Err(_) => break,
                };
                return Response::builder()
                    .status(StatusCode::OK)
                    .header("Content-Type", mime)
                    // Python FileResponse 정합: Accept-Ranges: bytes (range 지원 표시).
                    .header("Accept-Ranges", "bytes")
                    .header("Content-Security-Policy", "default-src 'none'")
                    .header("X-Content-Type-Options", "nosniff")
                    .body(axum::body::Body::from(bytes))
                    .unwrap();
            }
        }
    }
    err_response(ApiError::new("attachment_not_found", 404, "Attachment not found.")).into_response()
}

// --- WS /message-stream ------------------------------------------------------

#[derive(Debug, Deserialize)]
struct WsQuery {
    project_id: Option<String>,
    room_id: Option<String>,
    #[serde(default)]
    after: Option<String>,
}

async fn message_stream(
    ws: WebSocketUpgrade,
    State(s): State<AppState>,
    Query(q): Query<WsQuery>,
) -> Response {
    ws.on_upgrade(move |socket| handle_ws(socket, s, q))
}

async fn close_with(mut socket: WebSocket, code: u16, reason: &str) {
    let _ = socket
        .send(Message::Close(Some(CloseFrame {
            code,
            reason: reason.to_string().into(),
        })))
        .await;
}

async fn handle_ws(mut socket: WebSocket, s: AppState, q: WsQuery) {
    // project_id 필수 (DS-40): 없으면 close 4400.
    let project_id = match q.project_id.filter(|p| !p.is_empty()) {
        Some(p) => p,
        None => return close_with(socket, 4400, "missing project_id").await,
    };
    // room_id 지정 시 cross-project 방어: 미존재·불일치 모두 close 4404.
    let rooms: Option<HashSet<String>> = match &q.room_id {
        None => None,
        Some(rid) => match s.repo.get_room(rid).await {
            Ok(Some(room)) if room.project_id == project_id => {
                Some(HashSet::from([rid.clone()]))
            }
            _ => return close_with(socket, 4404, "room not found / cross-project").await,
        },
    };

    let mut rx = s.hub.subscribe();
    // Python message-stream 은 accept 후 ready 프레임 없이 바로 replay/live 만 보낸다.
    // QI-WG-030 gap replay: room 지정 + after cursor 면 연결 직후 그 cursor 이후 메시지를
    // _message_update_payload(WS) 형식으로 먼저 흘려보낸다(Python messages.py 정합).
    if let (Some(rid), Some(after)) = (q.room_id.as_ref(), q.after.as_ref()) {
        let after_ts = match after.split_once("|message:") {
            Some((ts, _)) => Some(ts.to_string()),
            None => Some(after.clone()),
        };
        if let Ok(msgs) = s.repo.updates_since(rid, after_ts.as_deref(), 200).await {
            for m in &msgs {
                let payload = json!({
                    "type": "message_update",
                    "cursor": format!("{}|message:{}", m.recorded_at, m.message_id),
                    "data": {
                        "update_id": format!("message:{}", m.message_id),
                        "room_id": m.room_id,
                        "correlation_id": m.correlation_id,
                        "update_type": message_update_type(&m.direction, &m.status),
                        "message": message_to_dict(m, &project_id, Some("websocket")),
                        "event": Value::Null,
                        "occurred_at": m.occurred_at,
                    },
                });
                if socket.send(Message::Text(payload.to_string())).await.is_err() {
                    return;
                }
            }
        }
    }

    // keepalive: Python message-stream 처럼 주기적 heartbeat 로 연결 유지(idle 끊김 방지).
    // 끊김→재연결 시 live 를 놓치고 replay 로 몰려 받는(묶여 옴) 증상 해소(QI-WG-026 동등).
    let mut heartbeat = tokio::time::interval(std::time::Duration::from_secs(15));
    heartbeat.tick().await; // 첫 즉시 tick 소비(연결 직후 불필요한 hb 방지)
    loop {
        tokio::select! {
            ev = rx.recv() => match ev {
                Ok(ev) => {
                    if ev.project_id != project_id { continue; }
                    let matched = match &rooms { None => true, Some(set) => set.contains(&ev.topic) };
                    if matched {
                        let txt = serde_json::to_string(&ev.payload).unwrap_or_default();
                        if socket.send(Message::Text(txt)).await.is_err() { break; }
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => break,
            },
            msg = socket.recv() => match msg {
                Some(Ok(Message::Close(_))) | None => break,
                Some(Err(_)) => break,
                Some(Ok(Message::Ping(p))) => {
                    // 클라이언트 ping → pong 응답(연결 유지)
                    if socket.send(Message::Pong(p)).await.is_err() { break; }
                }
                Some(Ok(_)) => {} // text/pong 무시
            },
            _ = heartbeat.tick() => {
                // 서버 heartbeat (Python {"type":"heartbeat"} 정합)
                if socket
                    .send(Message::Text("{\"type\":\"heartbeat\"}".to_string()))
                    .await
                    .is_err()
                {
                    break;
                }
            },
        }
    }
}

// --- app ---------------------------------------------------------------------

/// 인증 게이트 (DS-40 §21). 토큰 미설정 시 dev 오픈(Python 동등).
/// internal/* = collector_token, 그 외 = api_token. Bearer 헤더 또는 ?token=.
async fn auth_mw(
    State(s): State<AppState>,
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    let path = req.uri().path().to_string();
    let is_internal = path.starts_with("/api/webgui/internal/");
    let required = if is_internal { &s.collector_token } else { &s.api_token };
    if let Some(tok) = required {
        let auth = req.headers().get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("");
        let bearer = auth
            .strip_prefix("Bearer ")
            .or_else(|| auth.strip_prefix("bearer "))
            .unwrap_or("")
            .trim();
        let qtoken = req
            .uri()
            .query()
            .map(|q| q.split('&').any(|kv| kv == format!("token={tok}")))
            .unwrap_or(false);
        if bearer != tok && !qtoken {
            return (
                StatusCode::UNAUTHORIZED,
                Json(json!({ "ok": false, "error": { "code": "unauthorized", "message": "Authentication required." } })),
            )
                .into_response();
        }
    }
    next.run(req).await
}

fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/", get(root_meta))
        .route("/api/webgui/internal/hook/collect", post(hook_collect))
        .route("/api/webgui/internal/runtime-activity/collect", post(runtime_activity_collect))
        .route("/api/webgui/internal/rooms/:room_id/messages/collect", post(message_collect))
        .route("/api/webgui/internal/rooms/:room_id/events/collect", post(event_collect))
        .route("/api/webgui/messages", post(message_send))
        .route("/api/webgui/projects", get(projects_list))
        .route("/api/webgui/rooms", get(rooms_list))
        .route("/api/webgui/rooms/:room_id/messages", get(room_messages))
        .route("/api/webgui/rooms/:room_id/events", get(room_events))
        .route("/api/webgui/rooms/:room_id/read", post(room_mark_read))
        .route("/api/webgui/messages/:message_id", get(message_get))
        .route("/api/webgui/message-updates", get(message_updates_get))
        .route("/api/webgui/runtime/status", get(runtime_status_get))
        .route("/api/webgui/artifacts/changes", get(artifacts_changes))
        .route("/api/webgui/artifacts/tree", get(artifacts_tree))
        .route("/api/webgui/artifacts/file", get(artifacts_file))
        .route("/api/webgui/artifacts/file/stream", get(artifacts_stream))
        .route("/api/webgui/artifacts/write", post(artifacts_write))
        .route("/api/webgui/message-attachments/images", post(attachment_upload))
        .route("/api/webgui/message-attachments/:attachment_id/preview", get(attachment_preview))
        .route("/api/webgui/message-stream", get(message_stream))
        .layer(axum::middleware::from_fn_with_state(state.clone(), auth_mw))
        // 에러 envelope 후처리는 가장 바깥 layer (auth_mw 의 평문 401 등도 변환 대상).
        .layer(axum::middleware::from_fn(envelope_errors_mw))
        .with_state(state)
}

/// 산출물 변경 감시 → 변경 이벤트 버퍼 적재 + WS artifact_changed publish.
/// P4: documents 한 루트만 감시하던 것을 documents·system·brain **3루트**로 확장해,
/// 코드(system)·페르소나(brain) 패널 변경도 우측 패널 볼드 마킹이 즉시 뜨게 한다.
/// (Python artifact_watcher.ROOT_TYPE_SUBDIR = documents/system/persona 정합)
/// FE 계약 root_type: documents | system | persona (brain 서브디렉토리 → "persona").
fn spawn_artifact_watcher(
    buf: Arc<ArtifactChangeBuffer>,
    hub: Arc<WsHub>,
    base: &str,
    selected: Option<&str>,
) {
    let Some(pid) = selected.map(|s| s.to_string()) else {
        return;
    };
    // (서브디렉토리, FE root_type) — Python ROOT_TYPE_SUBDIR 동등.
    let roots: [(&str, &str); 3] =
        [("documents", "documents"), ("system", "system"), ("brain", "persona")];
    for (subdir, root_type) in roots {
        spawn_one_root_watcher(buf.clone(), hub.clone(), base, &pid, subdir, root_type);
    }
}

/// 단일 루트 감시자 생성·leak. 루트 부재 시 조용히 skip.
fn spawn_one_root_watcher(
    buf: Arc<ArtifactChangeBuffer>,
    hub: Arc<WsHub>,
    base: &str,
    pid: &str,
    subdir: &str,
    root_type: &'static str,
) {
    use notify::{EventKind, RecursiveMode, Watcher};
    let root = std::path::PathBuf::from(format!("{}/{}/{}", base.trim_end_matches('/'), pid, subdir));
    if !root.exists() {
        eprintln!("[watcher] {root_type} 루트 없음, skip: {}", root.display());
        return;
    }
    let root_cb = root.clone();
    let pid_cb = pid.to_string();
    let mut watcher = match notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
        let Ok(event) = res else { return };
        let change_type = match event.kind {
            EventKind::Create(_) => "created",
            EventKind::Remove(_) => "deleted",
            EventKind::Modify(_) => "modified",
            _ => return,
        };
        for path in &event.paths {
            let rel = match path.strip_prefix(&root_cb) {
                Ok(p) => p.to_string_lossy().replace('\\', "/"),
                Err(_) => continue,
            };
            if rel.is_empty() || rel.split('/').any(|s| s.starts_with('.')) {
                continue;
            }
            let data = json!({
                "project_id": pid_cb,
                "root_type": root_type,
                "change_type": change_type,
                "path": rel,
                "node_type": if path.is_dir() { "directory" } else { "file" },
                "coalesced": false,
            });
            let seq = buf.push(&pid_cb, data.clone());
            // WS envelope 은 Python artifact_watcher._emit 과 1:1 정합이어야 한다.
            //   Python: {"type":"artifact_changed","cursor":..,"data":<변경객체 그대로>}
            // FE(monitor.js handleWsEvent)는 최상위 env.type==="artifact_changed" 로 분기하고
            // applyArtifactChange(data) 가 data.root_type/data.path 를 직접 읽는다.
            // (기존 {"type":"message_update","data":{update_type,artifact}} 는 FE 가 메시지로
            //  오라우팅 → artifact 패널 미갱신. 이게 'Rust 산출물 반영 안 됨'의 근본 원인이었다.)
            let ws = json!({
                "type": "artifact_changed",
                "cursor": seq.to_string(),
                "data": data,
            });
            hub.publish("artifact", ws, &pid_cb);
        }
    }) {
        Ok(w) => w,
        Err(e) => {
            eprintln!("[watcher] {root_type} 생성 실패: {e}");
            return;
        }
    };
    if let Err(e) = watcher.watch(&root, RecursiveMode::Recursive) {
        eprintln!("[watcher] {root_type} watch 실패: {e}");
        return;
    }
    eprintln!("[watcher] watching [{root_type}] {}", root.display());
    Box::leak(Box::new(watcher)); // 프로그램 수명 동안 유지
}

#[tokio::main]
async fn main() {
    let database_url = std::env::var("AGITEAMAPP_DATABASE_URL").unwrap_or_else(|_| {
        "postgres://agiteamapp:agiteamapp_dev_pw@127.0.0.1:15432/agiteamapp".to_string()
    });
    let selected_project = std::env::var("AGITEAMAPP_PROJECT_ID").ok().or(Some("Panthea".to_string()));
    let projects_base =
        std::env::var("AGITEAMAPP_PROJECTS_BASE").unwrap_or_else(|_| "/Users/ppillip/Projects".to_string());

    let repo = PgRepository::connect(&database_url).await.expect("DB 연결 실패");

    // cmux discovery: 백그라운드로 `cmux tree` 폴링 → connection_state/projects 갱신.
    let discovery = Arc::new(DiscoveryRegistry::new());

    // mux 선택: AGITEAMAPP_MUX=cmux → 실 cmux(team CLI + discovery PM 해소), 그 외(기본) → dummy.
    let mux = match std::env::var("AGITEAMAPP_MUX").as_deref() {
        Ok("cmux") => {
            let cmux_bin = std::env::var("AGITEAMAPP_CMUX_BIN")
                .unwrap_or_else(|_| "/Applications/cmux.app/Contents/Resources/bin/cmux".to_string());
            let projects_base = std::env::var("AGITEAMAPP_PROJECTS_BASE")
                .unwrap_or_else(|_| "/Users/ppillip/Projects".to_string());
            MuxAdapter::Cmux(CmuxAdapter {
                cmux_bin,
                projects_base,
                discovery: Some(discovery.clone()),
            })
        }
        _ => MuxAdapter::Dummy(DummyMux::default()),
    };
    {
        let disc = discovery.clone();
        let cmux_bin = std::env::var("AGITEAMAPP_CMUX_BIN")
            .unwrap_or_else(|_| "/Applications/cmux.app/Contents/Resources/bin/cmux".to_string());
        tokio::spawn(async move {
            loop {
                if let Ok(out) = tokio::process::Command::new(&cmux_bin).arg("tree").output().await {
                    if out.status.success() {
                        let text = String::from_utf8_lossy(&out.stdout);
                        let now = std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .map(|d| d.as_secs() as i64)
                            .unwrap_or(0);
                        disc.refresh_from_tree(&text, now);
                    }
                }
                // 실시간성: discovery 폴링 1s (기존 5s → PM 응답 표시 지연 해소).
                // 환경변수 AGITEAMAPP_DISCOVERY_POLL_MS 로 조정 가능(기본 1000ms).
                let poll_ms = std::env::var("AGITEAMAPP_DISCOVERY_POLL_MS")
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(1000);
                tokio::time::sleep(std::time::Duration::from_millis(poll_ms)).await;
            }
        });
    }

    // 산출물 변경 watcher (WG-ART-04): selected 프로젝트 documents 루트 감시 → 버퍼+WS publish.
    // Python WEBGUI_ARTIFACT_WATCHER_ENABLED 대응. 미설정 기본 true(운영), equiv 는 false 로 끈다.
    let artifact_watcher_enabled = std::env::var("AGITEAMAPP_ARTIFACT_WATCHER_ENABLED")
        .map(|v| {
            let v = v.to_lowercase();
            v == "true" || v == "1" || v == "yes"
        })
        .unwrap_or(true);
    let changes = Arc::new(ArtifactChangeBuffer::new());
    let hub_for_watch = Arc::new(WsHub::new());
    if artifact_watcher_enabled {
        spawn_artifact_watcher(
            changes.clone(),
            hub_for_watch.clone(),
            &projects_base,
            selected_project.as_deref(),
        );
    }

    let state = AppState {
        repo: Arc::new(repo),
        activity: Arc::new(ActivityRegistry::new()),
        hub: hub_for_watch,
        mux: Arc::new(mux),
        discovery,
        changes,
        selected_project,
        projects_base,
        api_token: std::env::var("AGITEAMAPP_API_TOKEN").ok().filter(|s| !s.is_empty()),
        collector_token: std::env::var("AGITEAMAPP_COLLECTOR_TOKEN").ok().filter(|s| !s.is_empty()),
        artifact_watcher_enabled,
        tx_offsets: Arc::new(Mutex::new(HashMap::new())),
        tx_sessions: Arc::new(Mutex::new(HashMap::new())),
        tx_path_locks: Arc::new(Mutex::new(HashMap::new())),
    };

    // transcript 폴링 fallback 루프 (Python background.transcript_loop 등가).
    // hook_stop 즉시수집이 주 경로지만, PM 발화 직후 transcript flush 레이스로 Stop 시점에
    // 아직 안 써진 메시지는 다음 훅까지 정체된다. 이 루프가 등록된 세션을 짧은 주기로
    // 재수집해 [offset..EOF] 신규분을 즉시 흘려보낸다. offset 기반이라 idle 은 거의 무비용.
    {
        let repo = state.repo.clone();
        let hub = state.hub.clone();
        let offsets = state.tx_offsets.clone();
        let sessions = state.tx_sessions.clone();
        let path_locks = state.tx_path_locks.clone();
        let discovery = state.discovery.clone();
        tokio::spawn(async move {
            let collector = TranscriptCollector {
                repo,
                hub,
                offsets,
                sessions: sessions.clone(),
                path_locks,
            };
            let poll_ms = std::env::var("AGITEAMAPP_TRANSCRIPT_POLL_MS")
                .ok()
                .and_then(|v| v.parse::<u64>().ok())
                .filter(|n| *n > 0)
                .unwrap_or(1000);
            let mut tick: u64 = 0;
            loop {
                // 등록된 모든 (project, role) 세션을 재수집한다. role 키라 7 surface 각각
                // 정확히 1개 현재 세션만 폴링하고, offset 기반이라 idle 은 즉시 return(무비용).
                let hints: Vec<TranscriptHint> = {
                    let m = sessions.lock().unwrap();
                    m.values().cloned().collect()
                };
                for h in &hints {
                    collector.collect(h).await;
                }
                // 커버리지 교차검증(약 10초마다): discovery 가 connected 로 보는 surface 중
                // 아직 훅으로 transcript 가 등록되지 않은 것을 경고 로그로 가시화한다.
                // (discovery 에는 transcript_path 가 없어 그 자체로는 폴링할 수 없다 — 경로는
                //  훅에서만 온다. 모든 활성 에이전트는 부팅 시 SessionStart 훅으로 등록된다.)
                tick = tick.wrapping_add(1);
                if tick % 10 == 0 {
                    let registered: HashSet<(String, String)> = {
                        let m = sessions.lock().unwrap();
                        m.keys().cloned().collect()
                    };
                    let missing: Vec<(String, String)> = discovery
                        .connected_surfaces()
                        .into_iter()
                        .filter(|k| !registered.contains(k))
                        .collect();
                    if !missing.is_empty() {
                        eprintln!(
                            "[transcript:coverage] 미등록 surface(훅 대기중)={:?} 등록됨={}",
                            missing,
                            registered.len()
                        );
                    }
                }
                tokio::time::sleep(std::time::Duration::from_millis(poll_ms)).await;
            }
        });
    }

    let port: u16 = std::env::var("AGITEAMAPP_RS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8081);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));

    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind failed");
    println!("agiteamapp-http listening on http://{addr} (db={database_url})");
    axum::serve(listener, app(state)).await.expect("serve failed");
}
