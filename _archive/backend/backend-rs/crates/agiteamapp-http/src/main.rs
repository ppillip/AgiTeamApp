//! agiteamapp-http — axum adapter. 라우팅·핸들러는 core usecase 를 호출만 한다.
//! DB 구현은 agiteamapp-db(PgRepository), WS fanout 은 broadcast 기반 WsHub 로 주입.


use std::collections::HashSet;
use std::net::SocketAddr;
use std::sync::Arc;

use agiteamapp_core::{
    collect_event, collect_message, collect_runtime_activity,
    collect_transcript_records, get_message,
    list_events_uc, list_projects, list_room_messages, list_rooms, mark_read,
    message_to_dict, message_update_type, message_updates,
    runtime_status, send_message, ActivityRegistry, ApiError, ArtifactChangeBuffer,
    CollectEventRequest, CollectMessageRequest, DiscoveryRegistry, EventPublisher,
    MuxPort, NoopTranscript, RuntimeActivityCollectRequest,
    ArtifactService, SendMessageRequest,
    TranscriptRecordsEnvelope, WebguiRepository,
};
use agiteamapp_db::PgRepository;
use agiteamapp_mux::{build_mux_adapter, MuxAdapter, MuxConfig};
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

/// Phase 1: edge(teamwatch) 수신면. 출처중립 — backend 는 dumb receiver 로 project+role 기반
/// envelope(파일 미읽음)만 받아 room/hash/direction/correlation/dedup/store/WS 를 전부 산출한다.
/// 기존 room-scoped /rooms/:room_id/messages/collect 와 독립(추가 경로).
async fn messages_collect(
    State(s): State<AppState>,
    Json(env): Json<TranscriptRecordsEnvelope>,
) -> (StatusCode, Json<Value>) {
    match collect_transcript_records(s.repo.as_ref(), s.hub.as_ref(), env).await {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

async fn event_collect(
    State(s): State<AppState>,
    Path(room_id): Path<String>,
    Json(req): Json<CollectEventRequest>,
) -> (StatusCode, Json<Value>) {
    // DS-134: backend 는 dumb receiver. event 는 저장/WS publish 만 하고 transcript 파일은
    // 일절 읽지 않는다(hook_stop 의 파일수집 트리거를 NoopTranscript 로 무력화).
    // LIVE TRANSCRIPT 공급은 edge(teamwatch) → /messages/collect 단일 통로만 담당한다.
    match collect_event(s.repo.as_ref(), s.hub.as_ref(), &NoopTranscript, &room_id, req).await {
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
    // WG-MSG-06: 첨부 해소용 AttachmentService(project_root 기준) + 만료판정 기준시각.
    let att_svc =
        agiteamapp_core::AttachmentService::new(&project_root(&s.projects_base, &pid));
    match send_message(
        s.repo.as_ref(),
        s.mux.as_ref(),
        s.hub.as_ref(),
        &pid,
        &correlation_id,
        req,
        Some(&att_svc),
        now_epoch(),
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

/// root_type 정규화·검증 (Python _normalize_root_type 정합). 미지정/빈값 → documents, 미지의 값 → 400.
fn normalize_root_type(rt: Option<&str>) -> Result<String, ApiError> {
    let r = rt.unwrap_or("").trim().to_lowercase();
    if r.is_empty() {
        return Ok("documents".to_string());
    }
    if matches!(r.as_str(), "documents" | "system" | "persona") {
        Ok(r)
    } else {
        Err(ApiError::new("invalid_request", 400, "root_type must be one of: documents, system, persona"))
    }
}

// WG-ART-08 새파일 생성 (DS-132 §4). 폴더 한정 JSON. 201 + {file, tree_refresh}.
#[derive(Debug, Deserialize)]
struct CreateBody {
    parent_path: String,
    filename: String,
    #[serde(default = "default_template")]
    template: String,
    #[serde(default = "default_if_exists_error")]
    if_exists: String,
    project_id: Option<String>,
    root_type: Option<String>,
}
fn default_template() -> String {
    "empty".to_string()
}
fn default_if_exists_error() -> String {
    "error".to_string()
}

async fn artifacts_create_file(
    State(s): State<AppState>,
    Json(body): Json<CreateBody>,
) -> (StatusCode, Json<Value>) {
    let rt = match normalize_root_type(body.root_type.as_deref()) {
        Ok(v) => v,
        Err(e) => return err_response(e),
    };
    let svc = s.artifact_svc(body.project_id.as_deref(), Some(&rt));
    match svc.create_file(Some(&body.parent_path), &body.filename, &body.template, &body.if_exists, &rt, 1_048_576) {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

// WG-ART-09 파일 업로드 (DS-132 §5). 폴더 한정 multipart. 201 + {upload, file, tree_refresh}.
async fn artifacts_upload(
    State(s): State<AppState>,
    mut mp: axum::extract::Multipart,
) -> (StatusCode, Json<Value>) {
    let mut project_id: Option<String> = None;
    let mut root_type: Option<String> = None;
    let mut parent_path: Option<String> = None;
    let mut if_exists: Option<String> = None;
    let mut client_upload_id: Option<String> = None;
    let mut filename: Option<String> = None;
    let mut data: Vec<u8> = Vec::new();
    while let Ok(Some(field)) = mp.next_field().await {
        match field.name() {
            Some("project_id") => project_id = field.text().await.ok(),
            Some("root_type") => root_type = field.text().await.ok(),
            Some("parent_path") => parent_path = field.text().await.ok(),
            Some("if_exists") => if_exists = field.text().await.ok(),
            Some("client_upload_id") => client_upload_id = field.text().await.ok(),
            Some("file") => {
                filename = field.file_name().map(|s| s.to_string());
                if let Ok(b) = field.bytes().await {
                    data = b.to_vec();
                }
            }
            _ => {}
        }
    }
    let rt = match normalize_root_type(root_type.as_deref()) {
        Ok(v) => v,
        Err(e) => return err_response(e),
    };
    let parent = parent_path.unwrap_or_default();
    let if_exists = if_exists.unwrap_or_else(|| "rename".to_string());
    let fname = filename.unwrap_or_default();
    let svc = s.artifact_svc(project_id.as_deref(), Some(&rt));
    match svc.upload_file(
        Some(&parent),
        &fname,
        &data,
        &if_exists,
        &rt,
        client_upload_id.as_deref(),
        26_214_400,
        1_048_576,
    ) {
        Ok(data) => ok_201(data),
        Err(e) => err_response(e),
    }
}

// WG-ART-07: 산출물/코드/페르소나 파일 삭제 (FE 우클릭 '삭제' 메뉴).
// FE 계약(src/api/index.js deleteFile): POST /api/webgui/artifacts/delete
//   body { project_id?, root_type?, path }. 응답 {ok:true,data:{deleted:true,path}}.
#[derive(Debug, Deserialize)]
struct DeleteBody {
    path: String,
    project_id: Option<String>,
    root_type: Option<String>,
}

async fn artifacts_delete(
    State(s): State<AppState>,
    Json(body): Json<DeleteBody>,
) -> (StatusCode, Json<Value>) {
    let svc = s.artifact_svc(body.project_id.as_deref(), body.root_type.as_deref());
    // _archive 백업 파일명 타임스탬프(로컬시각 YYYYMMDDhhmmss).
    let timestamp = chrono::Local::now().format("%Y%m%d%H%M%S").to_string();
    match svc.delete_file(&body.path, &timestamp) {
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
    // WG-ART-03D 다운로드 확장: download=1|true → Content-Disposition: attachment.
    #[serde(default)]
    download: Option<String>,
    #[serde(default)]
    filename: Option<String>,
}

fn is_truthy(v: Option<&str>) -> bool {
    matches!(v.unwrap_or("").trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on")
}

/// 전체 percent-encoding (unreserved 외 모두 %xx). RFC 3986 unreserved 유지.
fn pct_encode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// path 표시용 인코딩(슬래시 보존). Python quote(path, safe="/") 정합.
fn pct_encode_path(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' => out.push(b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// RFC 6266/5987 attachment Content-Disposition (DS-132 §6.3).
fn content_disposition(filename: &str) -> String {
    let ascii: String = filename
        .chars()
        .map(|c| {
            let n = c as u32;
            if (32..127).contains(&n) && c != '"' && c != '\\' && c != '/' {
                c
            } else {
                '_'
            }
        })
        .collect();
    let ascii = ascii.trim();
    let ascii = if ascii.is_empty() { "download" } else { ascii };
    format!("attachment; filename=\"{ascii}\"; filename*=UTF-8''{}", pct_encode(filename))
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
    let is_download = is_truthy(q.download.as_deref());
    // WG-ART-03D 다운로드명: filename override 사용 시 파일명 규칙 검증 + 확장자 동일 강제(§6.1).
    let actual_name = abs.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let mut download_name = actual_name.clone();
    if let Some(fname) = q.filename.as_deref() {
        if !fname.trim().is_empty() {
            let override_name = match svc.validate_filename(fname) {
                Ok(v) => v,
                Err(e) => return err_response(e).into_response(),
            };
            let actual_ext = actual_name.rsplit_once('.').map(|(_, e)| e.to_lowercase()).unwrap_or_default();
            let override_ext = override_name.rsplit_once('.').map(|(_, e)| e.to_lowercase()).unwrap_or_default();
            if override_ext != actual_ext {
                return err_response(ApiError::new("invalid_path", 400, "Invalid artifact path.")).into_response();
            }
            download_name = override_name;
        }
    }
    let bytes = match std::fs::read(&abs) {
        Ok(b) => b,
        Err(_) => return err_response(ApiError::new("artifact_path_not_found", 404, "not found")).into_response(),
    };
    // QI-WG-048: size==0(빈 파일, empty 템플릿 생성물 등) 다운로드 시 `size as usize - 1` 언더플로 →
    // release 에서 usize::MAX 로 wrap → bytes[0..=usize::MAX] 슬라이스 OOB 패닉 → 연결 종료 버그.
    // saturating_sub + 슬라이스 가드로 0바이트도 200 + Content-Length:0 으로 안전 반환(Python 동등).
    let total = size as usize;
    let (mut start, mut end, mut status) = (0usize, total.saturating_sub(1), StatusCode::OK);
    let mut resp_headers = vec![
        ("Accept-Ranges".to_string(), "bytes".to_string()),
        ("Content-Type".to_string(), mime.clone()),
        ("X-Content-Type-Options".to_string(), "nosniff".to_string()),
        ("X-Artifact-Path".to_string(), pct_encode_path(&q.path)),
    ];
    if is_download {
        // 다운로드는 attachment 우선(html/svg 여도). RFC 6266 filename* 포함.
        resp_headers.push(("Content-Disposition".to_string(), content_disposition(&download_name)));
    }
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
            let e_val = e_str.trim().parse::<usize>().unwrap_or(total.saturating_sub(1)).min(total.saturating_sub(1));
            // total==0(빈 파일) 또는 범위 위반 → 416 (Python: start>=size 동일 처리).
            if total == 0 || s_val > e_val || s_val >= total {
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
    // 빈 파일은 빈 본문(슬라이스 가드). 그 외에는 [start..=end].
    let slice = if total == 0 { Vec::new() } else { bytes[start..=end].to_vec() };
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
        .route("/api/webgui/internal/runtime-activity/collect", post(runtime_activity_collect))
        .route("/api/webgui/internal/messages/collect", post(messages_collect))
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
        .route("/api/webgui/artifacts/create-file", post(artifacts_create_file))
        // 업로드는 25 MiB 상한 정책을 핸들러가 검증(413). axum 기본 2MB 본문 제한을 넘겨
        // 25 MiB 정상 업로드/26 MiB 초과 테스트가 핸들러까지 도달하도록 64 MiB 로 확장.
        .route(
            "/api/webgui/artifacts/upload",
            post(artifacts_upload).layer(axum::extract::DefaultBodyLimit::max(64 * 1024 * 1024)),
        )
        .route("/api/webgui/artifacts/delete", post(artifacts_delete))
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

    let repo = Arc::new(PgRepository::connect(&database_url).await.expect("DB 연결 실패"));

    // discovery: 백그라운드로 mux.tree() 폴링 → connection_state/projects 갱신.
    let discovery = Arc::new(DiscoveryRegistry::new());

    // mux 선택+구성은 팩토리(agiteamapp-mux)가 전담. main 은 어댑터 선택 규칙·transport 명령을 모른다.
    // 선택 규칙: AGITEAMAPP_MUX == "dummy" → Dummy, 그 외(미설정·레거시 포함) → Team(team facade 경유).
    let mux_config = MuxConfig::from_env();
    let mux = Arc::new(build_mux_adapter(&mux_config, Some(discovery.clone())));
    {
        let disc = discovery.clone();
        let mux_for_loop = mux.clone();
        let repo_for_loop = repo.clone();
        // 실시간성: discovery 폴링 기본 1000ms(AGITEAMAPP_DISCOVERY_POLL_MS 로 조정).
        let poll_ms = mux_config.discovery_poll_ms;
        tokio::spawn(async move {
            loop {
                // 포트 경유 — transport(mux) 직접 호출 금지. tree() 가 Err 면 갱신 건너뜀(직전 상태 보존).
                if let Ok(workspaces) = mux_for_loop.tree().await {
                    let now = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_secs() as i64)
                        .unwrap_or(0);
                    disc.refresh_from_workspaces(&workspaces, now);
                    // room.display_name 을 discovery 해소 별칭으로 동기화(역할명 표시 결함 정정).
                    // 방이 없으면 no-op(신규 방은 생성 후 다음 폴에서 자동 정정 → 회귀 방지).
                    // 가드(빈값/role 동일/무변화)는 repo 구현이 처리하므로 매 폴 호출해도 무의미 write 없음.
                    for (project_id, role_id, display_name) in disc.connected_display_names() {
                        let _ = repo_for_loop
                            .update_room_display_name(&project_id, &role_id, &display_name)
                            .await;
                    }
                }
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
        repo,
        activity: Arc::new(ActivityRegistry::new()),
        hub: hub_for_watch,
        mux,
        discovery,
        changes,
        selected_project,
        projects_base,
        api_token: std::env::var("AGITEAMAPP_API_TOKEN").ok().filter(|s| !s.is_empty()),
        collector_token: std::env::var("AGITEAMAPP_COLLECTOR_TOKEN").ok().filter(|s| !s.is_empty()),
        artifact_watcher_enabled,
    };

    // DS-134: backend 의 중앙 transcript 수집(폴링 fallback)은 제거됐다. LIVE TRANSCRIPT 는
    // 전적으로 edge(teamwatch)가 파일을 tail 해 /api/webgui/internal/messages/collect 로
    // POST 하는 단일 통로로만 공급된다. backend 는 파일/offset/세션레지스트리를 모르는
    // dumb receiver 다(분산환경에서 타 프로젝트 파일 접근 불가 문제 해소).

    let port: u16 = std::env::var("AGITEAMAPP_RS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8081);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));

    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind failed");
    println!("agiteamapp-http listening on http://{addr} (db={database_url})");

    // teamwatch(runtime-activity 폴러)는 backend 가 spawn/supervise 하지 않는다.
    // backend 는 수신 엔드포인트(/api/webgui/internal/runtime-activity/collect)로 POST 만 받고,
    // teamwatch 는 외부(teamboot/수동)에서 1프로젝트·1팀 단위로 독립 실행된다(폴러 부재 허용).
    axum::serve(listener, app(state))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("serve failed");
}

/// Ctrl-C 또는 SIGTERM 수신 시 graceful shutdown 시작.
async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        if let Ok(mut sig) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            sig.recv().await;
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {}
        _ = term => {}
    }
    eprintln!("[shutdown] 종료 시그널 수신 → graceful shutdown");
}
