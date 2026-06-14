// 백엔드 응답 → FE 도메인 모델 정규화 (순수 함수, import.meta 미사용 → node 테스트 가능).
//
// 백엔드 스키마 정정이 진행 중이므로(제우스 지시) 방어적으로 흡수한다:
//  - 식별 role 은 `role ?? role_id` (DS-40 명세 vs 직렬화기 차이)
//  - projects 응답은 registry형({roles:[...]}) ↔ ProjectSummary형 둘 다 수용
//  - surface_id 는 도메인 모델에 담지 않는다 (PM 확정: 화면 비노출, 식별은 (project_id, role))
//
// 역할 표준 순서·한글 별칭(DS-50 §2 방 목록 기준).
export const ROLE_ORDER = ["PM", "Architect", "DeveloperBE", "DeveloperFE", "Designer", "QA", "DevOps"];

const ROLE_LABEL = {
  PM: "PM",
  Architect: "Architect",
  DeveloperBE: "BE",
  DeveloperFE: "FE",
  Designer: "Design",
  QA: "QA",
  DevOps: "DevOps",
};

export function roleLabel(role) {
  return ROLE_LABEL[role] || role || "?";
}

export function roleOrder(role) {
  const idx = ROLE_ORDER.indexOf(role);
  return idx === -1 ? 99 : idx;
}

function pickRole(o) {
  return o.role ?? o.role_id ?? null;
}

// ── provenance(출처) 표식 (DS-60 §6.1 신뢰/provenance 규칙) ──────────
// source/kind → 한눈에 신뢰 가능한 UI 표식. 실데이터(live/sent) ↔ 수동(manual) ↔ 목업(mock) ↔ 진단(diag).
//   hook=LIVE HOOK · transcript=LIVE TRANSCRIPT (실데이터)
//   webgui/pm_bridge/bridge=SENT (UI 발신·PM Bridge 제출)
//   manual/injected=MANUAL (운영자 수동, 실 hook 위장 금지)
//   mock=MOCK (is_mock/is_real_data=false 고정)
//   read_screen/raw_log_collector=DIAGNOSTIC (본문 canonical 승격 금지)
const PROVENANCE = {
  hook: { label: "LIVE HOOK", tone: "live" },
  transcript: { label: "LIVE TRANSCRIPT", tone: "live" },
  webgui: { label: "SENT", tone: "sent" },
  pm_bridge: { label: "SENT", tone: "sent" },
  bridge: { label: "SENT", tone: "sent" },
  manual: { label: "MANUAL", tone: "manual" },
  injected: { label: "MANUAL", tone: "manual" },
  mock: { label: "MOCK", tone: "mock" },
  read_screen: { label: "DIAGNOSTIC", tone: "diag" },
  raw_log_collector: { label: "DIAGNOSTIC", tone: "diag" },
  role_log: { label: "DIAGNOSTIC", tone: "diag" },
  raw_log: { label: "DIAGNOSTIC", tone: "diag" },
  log: { label: "DIAGNOSTIC", tone: "diag" },
};
// tone 별 실데이터 여부 (유저가 '진짜 hook 데이터'를 신뢰하도록 구분)
const REAL_TONES = new Set(["live", "sent"]);

// source(+is_mock/kind) → {label, tone, real}. 알 수 없는 source 는 표식 없음(null).
export function provenanceInfo(source, { isMock = false, kind = null } = {}) {
  if (isMock || kind === "mock" || source === "mock") {
    return { label: "MOCK", tone: "mock", real: false };
  }
  const p = source ? PROVENANCE[source] : null;
  if (!p) return { label: null, tone: "unknown", real: false };
  return { ...p, real: REAL_TONES.has(p.tone) };
}

// 연결/런타임 상태 → 3종 표식 (DS-60 §4.4 라이브 디스커버리 상태도).
//   mock/runtime_state=mock          → MOCK (목업 명시, 실데이터 위장 금지)
//   connected 또는 runtime_state=live → LIVE
//   그 외(disconnected/unknown)        → 끊김
export function connectionInfo(connectionState, runtimeState, { mock = false } = {}) {
  if (mock || runtimeState === "mock") return { label: "MOCK", tone: "mock" };
  if (connectionState === "connected" || runtimeState === "live") return { label: "LIVE", tone: "live" };
  return { label: "끊김", tone: "off" };
}

// 표시용 모노그램(아바타 1글자): display_name 첫 글자, 없으면 role 약어.
function monogram(displayName, role) {
  const n = (displayName || "").trim();
  if (n) return n[0];
  return roleLabel(role)[0] || "?";
}

// ── 프로젝트 ────────────────────────────────────────────────
// registry형: {project_id, workspace_id, roles:[{role_id, display_name, connection_state}], connected}
// ProjectSummary형: {project_id, workspace_title, connection_state, pm_connection_state, room_count}
export function adaptProject(p) {
  const roles = Array.isArray(p.roles) ? p.roles : [];
  const pmRole = roles.find((r) => pickRole(r) === "PM");
  const connected =
    p.connected ??
    p.connection_state === "connected" ??
    roles.some((r) => r.connection_state === "connected");
  const pmConnected =
    p.pm_connection_state === "connected" ||
    (pmRole ? pmRole.connection_state === "connected" : connected);
  return {
    projectId: p.project_id,
    title: p.workspace_title || p.project_id,
    connected: !!connected,
    pmConnected: !!pmConnected,
    roomCount: p.room_count ?? roles.length,
  };
}

export function adaptProjects(data) {
  const list = Array.isArray(data?.projects) ? data.projects : [];
  const projects = list.map(adaptProject);
  return { projects, selectedProjectId: data?.selected_project_id ?? projects[0]?.projectId ?? null };
}

// ── 방(room) ────────────────────────────────────────────────
export function adaptRoom(r) {
  const role = pickRole(r);
  const roomType = r.room_type || (role === "PM" ? "pm" : "role");
  const last = r.last_message || null;
  const prov = r.provenance || {};
  // 백엔드 room provenance 는 출처를 `origin` 키로 보낸다(메시지는 top-level source 도 동봉).
  // source(구 키) → origin(현행 키) → 평면 source 순으로 방어적 흡수. 이 누락이 provSource=null →
  // 팀뷰 출처배지가 연결축과 같은 'LIVE' 로 폴백되던 중복(LIVE 2개)의 근인이었다.
  const provSource = prov.source || prov.origin || r.source || null;
  const isMock = r.is_mock === true || prov.kind === "mock" || provSource === "mock";
  const runtimeState = r.runtime_state || prov.runtime_state || "unknown";
  return {
    roomId: r.room_id,
    projectId: r.project_id,
    role,
    roomType, // 'pm' | 'role'
    displayName: r.display_name || role,
    mono: monogram(r.display_name, role),
    connectionState: r.connection_state || "unknown",
    runtimeState, // live | disconnected | mock | unknown (DS-60 §4.4)
    // 런타임 활동(요구사항 15-1): 에이전트가 실시간으로 출력을 내는지(연결상태 위 2차원).
    //   active | idle | unknown. REST runtime_activity 매핑, WS runtime_activity_changed 로 실시간 갱신.
    runtimeActivity: r.runtime_activity || "unknown",
    readyState: r.ready_state || "unknown",
    collectorState: r.collector_state || "unknown",
    // provenance (DS-60 §6.1) — 방 단위 출처/신뢰
    provSource,
    isMock,
    isRealData: prov.is_real_data ?? null,
    teamSessionId: r.team_session_id || null,
    unread: r.unread_count ?? 0,
    lastText: last?.text ?? "",
    lastAt: r.last_message_at || last?.occurred_at || null,
    isPM: roomType === "pm" || role === "PM",
  };
}

export function adaptRooms(data) {
  const list = Array.isArray(data?.rooms) ? data.rooms : [];
  return list
    .map(adaptRoom)
    .sort((a, b) => roleOrder(a.role) - roleOrder(b.role));
}

// ── 메시지 ──────────────────────────────────────────────────
// out(우측, 내/PM 발신) = direction==='outbound'. inbound(좌측, 에이전트 응답).
//
// source 스키마는 BE 수집기 재작성(transcript/hook canonical) 진행 중이라
// 신·구 값을 모두 방어적으로 흡수한다(임의 추정 없이 passthrough + 파생 플래그).
//   canonical 본문: bridge/pm_bridge(발신), transcript(수신), hook
//   진단 보조(비canonical): read_screen, role_log, raw_log_collector, log
const CANONICAL_SOURCES = ["bridge", "pm_bridge", "transcript", "webgui", "hook"];
const DIAGNOSTIC_SOURCES = ["read_screen", "role_log", "raw_log_collector", "log", "raw_log"];

// MessageAttachment(DS-40 §4.2.1) → 카멜케이스 표시 모델. preview_url 만 화면 노출(절대경로 없음).
export function adaptAttachment(a) {
  if (!a) return null;
  return {
    attachmentId: a.attachment_id,
    clientAttachmentId: a.client_attachment_id || null,
    kind: a.kind || "image",
    filename: a.filename || null,
    mimeType: a.mime_type || null,
    sizeBytes: a.size_bytes ?? null,
    width: a.width ?? null,
    height: a.height ?? null,
    previewUrl: a.preview_url || null,
    expiresAt: a.expires_at || null,
  };
}

export function adaptMessage(m) {
  const role = pickRole(m);
  // 질문(user)=우측, 답변(assistant)=좌측. 방향이 명시되면 그대로 따르고,
  // 방향이 누락된 경우(WS 희소 페이로드 등) message_type 으로 보강한다.
  //   user_message(질문) → outbound(우측), assistant_message(답변) → inbound(좌측)
  const out =
    m.direction === "outbound" ||
    (m.direction == null && m.message_type === "user_message");
  // provenance 객체(신 스키마)를 우선 흡수하되, 평면 source(구 스키마)도 방어적으로 수용.
  const prov = m.provenance || {};
  const source = prov.source || m.source;
  const isMock = m.is_mock === true || prov.kind === "mock" || source === "mock";
  const isRealData = m.is_real_data ?? prov.is_real_data ?? null;
  // degraded: read-screen 스냅샷 보강 등 비canonical 경로로 채워진 본문(DS-60 §6.6).
  const degraded =
    m.status === "degraded" ||
    m.degraded === true ||
    source === "read_screen" ||
    m.message_type === "degraded";
  // 진단 보조 출처(어두운 아바타·보조 라벨 대상)
  const diagnostic = DIAGNOSTIC_SOURCES.includes(source);
  // 출처 표식(DS-60 §6.1): LIVE HOOK/TRANSCRIPT/SENT/MANUAL/MOCK/DIAGNOSTIC
  const prv = provenanceInfo(source, { isMock, kind: prov.kind });
  return {
    messageId: m.message_id,
    roomId: m.room_id,
    correlationId: m.correlation_id || null,
    role,
    direction: m.direction, // outbound | inbound | system
    source, // bridge | transcript | hook | webgui | read_screen | role_log | ...
    canonical: source == null ? true : CANONICAL_SOURCES.includes(source),
    diagnostic,
    // provenance 파생 (유저가 실/수동/목업을 한눈에)
    provLabel: prv.label, // 'LIVE HOOK' | 'LIVE TRANSCRIPT' | 'SENT' | 'MANUAL' | 'MOCK' | 'DIAGNOSTIC' | null
    provTone: prv.tone, // live | sent | manual | mock | diag | unknown
    isRealData: isRealData ?? prv.real,
    isMock,
    teamSessionId: m.team_session_id || null,
    messageType: m.message_type, // user_message | assistant_message | status | error | unmatched | log_line
    text: m.text ?? "",
    status: m.status, // pending | sent | failed | received | streaming | unmatched | degraded | ...
    out,
    occurredAt: m.occurred_at,
    recordedAt: m.recorded_at,
    pending: m.status === "pending",
    failed: m.status === "failed",
    unmatched: m.status === "unmatched" || m.message_type === "unmatched",
    degraded,
    // 이미지 첨부(DV-91): 순서 보존. preview_url 만 노출(호스트 절대경로 없음).
    attachments: Array.isArray(m.attachments) ? m.attachments.map(adaptAttachment).filter(Boolean) : [],
  };
}

export function adaptMessages(list) {
  return (Array.isArray(list) ? list : []).map(adaptMessage);
}

// ── 산출물 노드 ─────────────────────────────────────────────
export function adaptNode(n) {
  return {
    path: n.path,
    name: n.name,
    isDir: n.node_type === "directory",
    ext: n.extension || null,
    sizeBytes: n.size_bytes ?? null,
    hasChildren: !!n.has_children,
    renderable: !!n.renderable,
    children: Array.isArray(n.children) ? n.children.map(adaptNode) : null,
  };
}

// ── 산출물 파일 메타 ────────────────────────────────────────
export function adaptFile(file) {
  return {
    path: file.path,
    name: file.name,
    ext: file.extension,
    mime: file.mime_type,
    sizeBytes: file.size_bytes,
    renderMode: file.render_mode, // markdown | code | pdf_stream | html | image | converted_preview | unsupported
    languageHint: file.language_hint ?? null, // code 뷰어 언어팩 선택 힌트(BE 우선, FE 확장자 fallback)
    content: file.content ?? null,
    streamUrl: file.stream_url || null,
    convertedUrl: file.converted_url || null,
    sanitized: !!file.sanitized,
    warnings: Array.isArray(file.render_warnings) ? file.render_warnings : [],
  };
}
