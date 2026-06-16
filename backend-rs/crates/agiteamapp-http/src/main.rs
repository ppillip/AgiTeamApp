//! agiteamapp-http — axum adapter. 라우팅·핸들러는 core usecase 를 호출만 한다.
//! DB 구현은 agiteamapp-db(PgRepository), WS fanout 은 broadcast 기반 WsHub 로 주입.

use std::collections::HashSet;
use std::net::SocketAddr;
use std::sync::Arc;

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

/// transcript 즉시수집: hint.transcript_path 파일 읽어 파싱→store. (Python TranscriptCollector 동등)
struct TranscriptCollector {
    repo: Arc<PgRepository>,
    hub: Arc<WsHub>,
}
impl TranscriptPort for TranscriptCollector {
    async fn collect(&self, hint: &TranscriptHint) {
        let Some(path) = &hint.transcript_path else {
            return;
        };
        let text = match tokio::fs::read_to_string(path).await {
            Ok(t) => t,
            Err(e) => {
                eprintln!("[transcript] read 실패(무시) {path}: {e}");
                return;
            }
        };
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
            Ok(n) => eprintln!("[transcript] room={} stored={n}", hint.room_id),
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
}

impl AppState {
    fn transcript(&self) -> TranscriptCollector {
        TranscriptCollector { repo: self.repo.clone(), hub: self.hub.clone() }
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
                Some(Ok(_)) => {} // ping/text 무시
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

/// 산출물 documents 루트 감시 → 변경 이벤트 버퍼 적재 + WS artifact_changed publish.
fn spawn_artifact_watcher(
    buf: Arc<ArtifactChangeBuffer>,
    hub: Arc<WsHub>,
    base: &str,
    selected: Option<&str>,
) {
    use notify::{EventKind, RecursiveMode, Watcher};
    let Some(pid) = selected.map(|s| s.to_string()) else {
        return;
    };
    let root = std::path::PathBuf::from(format!("{}/{}/documents", base.trim_end_matches('/'), pid));
    if !root.exists() {
        eprintln!("[watcher] documents 루트 없음, skip: {}", root.display());
        return;
    }
    let root_cb = root.clone();
    let pid_cb = pid.clone();
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
                "root_type": "documents",
                "change_type": change_type,
                "path": rel,
                "node_type": if path.is_dir() { "directory" } else { "file" },
                "coalesced": false,
            });
            let seq = buf.push(&pid_cb, data.clone());
            let ws = json!({
                "type": "message_update",
                "cursor": seq.to_string(),
                "data": { "update_type": "artifact_changed", "artifact": data },
            });
            hub.publish("artifact", ws, &pid_cb);
        }
    }) {
        Ok(w) => w,
        Err(e) => {
            eprintln!("[watcher] 생성 실패: {e}");
            return;
        }
    };
    if let Err(e) = watcher.watch(&root, RecursiveMode::Recursive) {
        eprintln!("[watcher] watch 실패: {e}");
        return;
    }
    eprintln!("[watcher] watching {}", root.display());
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
            let team_bin = std::env::var("AGITEAMAPP_TEAM_BIN").unwrap_or_else(|_| "team".to_string());
            let projects_base = std::env::var("AGITEAMAPP_PROJECTS_BASE")
                .unwrap_or_else(|_| "/Users/ppillip/Projects".to_string());
            MuxAdapter::Cmux(CmuxAdapter {
                team_bin,
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
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
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
    };

    let port: u16 = std::env::var("AGITEAMAPP_RS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8081);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));

    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind failed");
    println!("agiteamapp-http listening on http://{addr} (db={database_url})");
    axum::serve(listener, app(state)).await.expect("serve failed");
}
