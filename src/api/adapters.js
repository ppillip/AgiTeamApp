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
  return {
    roomId: r.room_id,
    projectId: r.project_id,
    role,
    roomType, // 'pm' | 'role'
    displayName: r.display_name || role,
    mono: monogram(r.display_name, role),
    connectionState: r.connection_state || "unknown",
    readyState: r.ready_state || "unknown",
    collectorState: r.collector_state || "unknown",
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
export function adaptMessage(m) {
  const role = pickRole(m);
  const out = m.direction === "outbound";
  return {
    messageId: m.message_id,
    roomId: m.room_id,
    correlationId: m.correlation_id || null,
    role,
    direction: m.direction, // outbound | inbound | system
    source: m.source, // webgui | role_log | hook | ...
    messageType: m.message_type, // user_message | log_line | status | error | unmatched
    text: m.text ?? "",
    status: m.status, // pending | sent | failed | received | streaming | unmatched | ...
    out,
    occurredAt: m.occurred_at,
    recordedAt: m.recorded_at,
    pending: m.status === "pending",
    failed: m.status === "failed",
    unmatched: m.status === "unmatched" || m.message_type === "unmatched",
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
    renderMode: file.render_mode, // markdown | pdf_stream | converted_preview | unsupported
    content: file.content ?? null,
    streamUrl: file.stream_url || null,
    convertedUrl: file.converted_url || null,
    sanitized: !!file.sanitized,
    warnings: Array.isArray(file.render_warnings) ? file.render_warnings : [],
  };
}
