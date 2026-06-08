// 모니터 중앙 스토어 (Vue reactive). 컴포넌트는 이 store 와 actions 를 가져다 쓴다.
//
// 책임:
//  - 프로젝트 로드/선택/전환 (WG-PROJ-01) → 선택 project_id 기준으로 방·대화·산출물 재로드
//  - 방 목록(WG-CHAT-01), 방 선택 시 메시지(WG-CHAT-02)·읽음(WG-CHAT-03)
//  - PM 방 전용 송신(WG-MSG-02, 낙관적 UI), 팀원 방은 읽기전용
//  - 실시간 갱신: WebSocket(WG-MSG-05) 우선, 실패 시 polling(WG-MSG-04) 폴백
//  - 산출물 트리 lazy 펼침(WG-ART-01), 파일 뷰어(WG-ART-02)
//  - 백엔드 미연결 시 degraded=true → mock 데이터로 화면 유지(구조 우선 원칙)
//
// 식별: (projectId, role). surface 는 저장/표시하지 않는다.

import { reactive } from "vue";
import * as api from "../api/index.js";
import { ApiError } from "../api/client.js";
import { adaptMessage, adaptRoom, roleOrder } from "../api/adapters.js";
import {
  MOCK_PROJECTS,
  MOCK_ROOMS,
  MOCK_MESSAGES,
  MOCK_TREE_ROOT,
  MOCK_FILE,
} from "../data/mock.js";

let clientSeq = 0;
const nextClientId = () => `c_${Date.now()}_${++clientSeq}`;

export const store = reactive({
  // 연결/모드
  degraded: false, // true = 백엔드 미연결, mock 표시
  bootError: null,

  // 프로젝트
  projects: [],
  selectedProjectId: null,
  projectsLoading: false,

  // 방
  rooms: [],
  roomsLoading: false,
  selectedRoomId: null,

  // 대화
  messages: [],
  messagesLoading: false,
  draft: "",
  sending: false,
  sendError: null,
  // 페이지네이션(WG-CHAT-02): 최초 20개 + 위로 스크롤 더보기(before-cursor)
  messagesCursor: null, // 더 과거를 가리키는 next_cursor
  messagesHasMore: false,
  loadingOlder: false,

  // 전체 팀원 보기(UI-04): 방별 최근 말풍선 미리보기 캐시 { roomId: [message] }
  roomPreviews: {},
  previewsLoading: false,

  // 산출물 트리
  treeRoot: null,
  treeLoading: false,
  expanded: {}, // path -> true
  childrenCache: {}, // path -> [node]
  childrenLoading: {}, // path -> true

  // 뷰어
  viewer: { open: false, loading: false, path: null, file: null, error: null },
});

// ── 파생 getter (computed 대용 함수) ─────────────────────────
export function selectedRoom() {
  return store.rooms.find((r) => r.roomId === store.selectedRoomId) || null;
}
export function selectedProject() {
  return store.projects.find((p) => p.projectId === store.selectedProjectId) || null;
}
export function canCompose() {
  const r = selectedRoom();
  return !!r && r.isPM; // PM 방에서만 입력 가능
}

// ── 실시간 채널 (WG-MSG-04/05, DS-60 §4.4) ────────────────────
// 프로젝트 단위 WebSocket 1개를 구독한다. 창을 열어두면 백엔드가 발행하는
//   project_discovered / room_upserted / room_connection_changed / message(_sent/_received/_failed)
// 이벤트로 방·말풍선이 실시간으로 추가/갱신된다("파바바박"). WS 불가 시 polling 폴백:
//   - 선택방 message-updates(말풍선) + rooms 주기 재조회(방 추가·연결상태).
let ws = null;
let pollTimer = null; // 선택방 메시지 polling
let roomsPollTimer = null; // 방 목록/연결상태 polling
let pollCursor = null;
let wsProjectId = null;

function stopRealtime() {
  if (ws) {
    try {
      ws.close(1000);
    } catch {}
    ws = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (roomsPollTimer) {
    clearInterval(roomsPollTimer);
    roomsPollTimer = null;
  }
  pollCursor = null;
  wsProjectId = null;
}

// WS envelope → 이벤트 분기. 다양한 키(type/event_type/update_type)를 방어적으로 흡수.
function handleWsEvent(ev) {
  let env;
  try {
    env = JSON.parse(ev.data);
  } catch {
    return;
  }
  const data = env.data && typeof env.data === "object" ? env.data : env;
  const type = env.type || env.event_type || env.update_type || data.update_type || data.type;
  if (!type) return;
  if (type === "project_discovered") return upsertProjectEvent(data.project || data);
  if (type === "room_upserted") return upsertRoomEvent(data.room || data);
  if (type === "room_connection_changed") return applyRoomConnection(data.room || data);
  // message 계열: update envelope({update_type,room_id,message}) 형태로 정규화 후 머지
  if (type.indexOf("message") === 0 || data.message) {
    const update = data.update_type
      ? data
      : { update_type: type, room_id: data.room_id, message: data.message, occurred_at: data.occurred_at };
    applyUpdates([update]);
  }
}

// 새 프로젝트 발견 → projects 목록 upsert(현재 선택 유지)
function upsertProjectEvent(raw) {
  if (!raw || !raw.project_id) return;
  const idx = store.projects.findIndex((p) => p.projectId === raw.project_id);
  // 간단 흡수(projects는 ProjectSwitcher 표시용) — 최소 필드만 갱신/추가
  const p = {
    projectId: raw.project_id,
    title: raw.workspace_title || raw.title || raw.project_id,
    connected: raw.connected ?? raw.connection_state === "connected",
    pmConnected: raw.pm_connection_state === "connected",
    roomCount: raw.room_count ?? 0,
  };
  if (idx >= 0) store.projects[idx] = { ...store.projects[idx], ...p };
  else store.projects.push(p);
}

// 방 upsert(room_id 우선, 없으면 project_id+role) — 현재 선택 프로젝트만 반영
function upsertRoomEvent(raw) {
  if (!raw) return;
  const room = adaptRoom(raw);
  if (room.projectId && room.projectId !== store.selectedProjectId) return;
  const idx = store.rooms.findIndex(
    (r) => r.roomId === room.roomId || (r.projectId === room.projectId && r.role === room.role)
  );
  if (idx >= 0) {
    store.rooms[idx] = { ...store.rooms[idx], ...room };
  } else {
    store.rooms.push(room);
    store.rooms.sort((a, b) => roleOrder(a.role) - roleOrder(b.role)); // 역할 순서 유지
  }
}

// 연결상태 변경(삭제하지 않고 상태만 갱신, DS-60 §4.4)
function applyRoomConnection(raw) {
  if (!raw) return;
  const r = store.rooms.find((x) => x.roomId === raw.room_id || (x.projectId === raw.project_id && x.role === raw.role));
  if (!r) return;
  if (raw.connection_state) r.connectionState = raw.connection_state;
  if (raw.runtime_state) r.runtimeState = raw.runtime_state;
}

// rooms 재조회 결과를 기존 목록에 머지(연결상태/last 갱신 + 신규 방 추가) — polling 폴백용
function mergeRooms(rooms) {
  for (const room of rooms) {
    const idx = store.rooms.findIndex(
      (r) => r.roomId === room.roomId || (r.projectId === room.projectId && r.role === room.role)
    );
    if (idx >= 0) store.rooms[idx] = { ...store.rooms[idx], ...room };
    else store.rooms.push(room);
  }
  store.rooms.sort((a, b) => roleOrder(a.role) - roleOrder(b.role));
}

function startPolling(projectId) {
  if (pollTimer) clearInterval(pollTimer);
  pollCursor = null;
  pollTimer = setInterval(async () => {
    const rid = store.selectedRoomId;
    if (!rid) return;
    try {
      const { updates, next_cursor } = await api.fetchUpdates(rid, pollCursor);
      if (next_cursor) pollCursor = next_cursor;
      applyUpdates(updates);
    } catch {
      /* 다음 tick 재시도 */
    }
  }, 3000);
  if (roomsPollTimer) clearInterval(roomsPollTimer);
  roomsPollTimer = setInterval(async () => {
    if (store.selectedProjectId !== projectId) return;
    try {
      mergeRooms(await api.fetchRooms(projectId));
    } catch {}
  }, 5000);
}

function startRealtime(projectId) {
  stopRealtime();
  if (store.degraded || !projectId) return;
  wsProjectId = projectId;
  // 프로젝트 단위 WebSocket 우선(room_id 없이 프로젝트 전역 이벤트 구독)
  try {
    const url = api.messageStreamUrl(projectId);
    ws = new WebSocket(url);
    ws.onmessage = handleWsEvent;
    ws.onerror = () => {
      try {
        ws && ws.close();
      } catch {}
      ws = null;
      startPolling(projectId); // WS 불가 → polling 폴백
    };
    ws.onclose = (e) => {
      if (store.selectedProjectId === projectId && !pollTimer && e.code !== 1000) {
        startPolling(projectId);
      }
    };
  } catch {
    startPolling(projectId);
  }
}

// update_type → 누락 메타 보강 매핑.
// WS 브로드캐스트(transcript_collector)의 message 페이로드는 {message_id,text,status}
// 로 희소하여 direction/message_type/role/occurred_at 가 빠진다. 이를 envelope 의
// update_type·occurred_at 과 기존 메시지로 보강해야 질문(user)·답변(assistant)이
// 좌/우로 올바르게 렌더된다. (폴링 경로는 message_to_dict 로 전체 필드 → 보강은 무영향)
//   message_sent   → outbound / user_message      (질문, 우측)
//   message_received → inbound / assistant_message (답변, 좌측)
//   message_failed → outbound + status=failed
function enrichUpdateMessage(raw, updateType, occurredAt, existing) {
  const m = { ...raw };
  if (m.direction == null) {
    if (updateType === "message_received") m.direction = "inbound";
    else if (updateType === "message_sent" || updateType === "message_failed") m.direction = "outbound";
    else if (existing) m.direction = existing.direction;
  }
  if (m.message_type == null) {
    if (updateType === "message_received") m.message_type = "assistant_message";
    else if (updateType === "message_sent") m.message_type = "user_message";
    else if (existing) m.message_type = existing.messageType;
  }
  if (m.role == null && existing) m.role = existing.role;
  if (m.status == null && updateType === "message_failed") m.status = "failed";
  if (m.occurred_at == null && occurredAt) m.occurred_at = occurredAt;
  return m;
}

// 정의된(non-null) 값만 덮어써, 희소 갱신이 기존 메타(방향·역할·시각)를 지우지 않게 병합.
function mergeMessage(prev, next) {
  const merged = { ...prev };
  for (const k in next) {
    const v = next[k];
    if (v !== undefined && v !== null) merged[k] = v;
  }
  return merged;
}

// update(MessageUpdate) 목록을 머지. 선택된 방은 본문 스레드(store.messages)에,
// 그리고 전체 팀원 보기를 위해 해당 방의 미리보기(roomPreviews)에도 실시간 반영한다.
function applyUpdates(updates) {
  if (!Array.isArray(updates)) return;
  for (const u of updates) {
    if (!u || !u.message) continue; // 이벤트성(correlation_closed 등)은 본문 없음 → skip
    const rawId = u.message.message_id;
    // 1) 선택된 방의 본문 스레드 갱신
    if (u.room_id === store.selectedRoomId) {
      const idx = rawId != null ? store.messages.findIndex((x) => x.messageId === rawId) : -1;
      const existing = idx >= 0 ? store.messages[idx] : null;
      const m = adaptMessage(enrichUpdateMessage(u.message, u.update_type, u.occurred_at, existing));
      if (idx >= 0) store.messages[idx] = mergeMessage(store.messages[idx], m);
      else store.messages.push(m); // 신규 말풍선 → 즉시 추가(실시간 렌더)
    }
    // 2) 미리보기 갱신(전체 팀원 보기 — 선택 여부 무관, 모든 방)
    const pv = store.roomPreviews[u.room_id];
    if (pv) {
      const pi = rawId != null ? pv.findIndex((x) => x.messageId === rawId) : -1;
      const pm = adaptMessage(enrichUpdateMessage(u.message, u.update_type, u.occurred_at, pi >= 0 ? pv[pi] : null));
      if (pi >= 0) pv[pi] = mergeMessage(pv[pi], pm);
      else {
        pv.push(pm);
        while (pv.length > 8) pv.shift(); // 최근 몇 개만 유지
      }
    }
  }
}

// 전체 팀원 보기: 각 방의 최근 N개 말풍선을 병렬로 채운다(실데이터). degraded 시 mock.
export async function loadRoomPreviews(limit = 6) {
  const rooms = store.rooms.slice();
  if (!rooms.length) return;
  store.previewsLoading = true;
  try {
    if (store.degraded) {
      for (const r of rooms) {
        store.roomPreviews[r.roomId] = (MOCK_MESSAGES[r.roomId] || []).slice(-limit);
      }
      return;
    }
    await Promise.all(
      rooms.map(async (r) => {
        try {
          const { messages } = await api.fetchMessages(r.roomId, { limit });
          store.roomPreviews[r.roomId] = messages;
        } catch {
          store.roomPreviews[r.roomId] = [];
        }
      })
    );
  } finally {
    store.previewsLoading = false;
  }
}

// ── 액션 ────────────────────────────────────────────────────

// 연결 실패(백엔드 미기동/DB 미가동)인지 판정.
// 응답을 받았다면(빈 목록이어도) 백엔드는 '연결됨' → mock 금지. 진짜 도달 불가만 degraded.
//   network_error(status 0): fetch 자체 실패(백엔드 미기동)
//   503: DB 미가동 등 일시 불가(client.js §"DB 미가동 등 503 → degraded")
// 그 외(빈 응답·4xx·500 등)는 '연결됨'으로 보고 실데이터/빈 상태/에러로 처리한다.
function isOfflineError(e) {
  if (!(e instanceof ApiError)) return false;
  return e.code === "network_error" || e.status === 0 || e.status === 503;
}

export async function boot() {
  store.projectsLoading = true;
  store.bootError = null;
  try {
    const { projects, selectedProjectId } = await api.fetchProjects();
    // 성공 응답 = 백엔드 연결됨 → 실데이터 우선. 빈 목록이어도 mock 으로 덮지 않는다.
    store.degraded = false;
    store.bootError = null;
    store.projects = projects;
    if (projects.length) {
      await selectProject(selectedProjectId || projects[0].projectId);
    } else {
      // 진짜 빈 상태: 발견된 프로젝트 없음 → 빈 상태 UI(가짜 mock 노출 금지)
      store.selectedProjectId = null;
      store.rooms = [];
      store.selectedRoomId = null;
      store.messages = [];
      store.treeRoot = null;
    }
  } catch (e) {
    if (isOfflineError(e)) {
      // 백엔드 도달 불가 → degraded 모드(mock 으로 화면 유지)
      enterDegraded(e);
    } else {
      // 연결은 됐으나 응답 오류 → 에러 노출, mock 금지(빈 상태)
      store.degraded = false;
      store.bootError = e instanceof ApiError ? e.message : String(e?.message || e);
      store.projects = [];
      store.selectedProjectId = null;
      store.rooms = [];
      store.selectedRoomId = null;
      store.messages = [];
      store.treeRoot = null;
    }
  } finally {
    store.projectsLoading = false;
  }
}

function enterDegraded(e) {
  store.degraded = true;
  store.bootError = e instanceof ApiError ? e.message : String(e?.message || e);
  store.projects = MOCK_PROJECTS;
  store.selectedProjectId = MOCK_PROJECTS[0].projectId;
  store.rooms = MOCK_ROOMS[store.selectedProjectId] || [];
  store.selectedRoomId = store.rooms[0]?.roomId || null;
  store.messages = MOCK_MESSAGES[store.selectedRoomId] || [];
  store.treeRoot = MOCK_TREE_ROOT;
}

export async function selectProject(projectId) {
  if (!projectId) return;
  store.selectedProjectId = projectId;
  stopRealtime();
  store.selectedRoomId = null;
  store.messages = [];
  store.roomPreviews = {}; // 전체 팀원 보기 미리보기 초기화
  // 산출물 트리 초기화 후 재로드
  store.treeRoot = null;
  store.expanded = {};
  store.childrenCache = {};
  closeViewer();

  if (store.degraded) {
    store.rooms = MOCK_ROOMS[projectId] || [];
    store.treeRoot = MOCK_TREE_ROOT;
    if (store.rooms[0]) await selectRoom(store.rooms[0].roomId);
    return;
  }

  store.roomsLoading = true;
  try {
    const rooms = await api.fetchRooms(projectId);
    store.rooms = rooms;
    // 기본 선택: PM 방
    const pm = rooms.find((r) => r.isPM) || rooms[0];
    if (pm) await selectRoom(pm.roomId);
  } catch (e) {
    store.rooms = [];
  } finally {
    store.roomsLoading = false;
  }
  // 프로젝트 단위 실시간 구독 시작(방·말풍선 실시간 추가)
  startRealtime(projectId);
  loadTreeRoot();
}

export async function selectRoom(roomId) {
  if (!roomId) return;
  store.selectedRoomId = roomId;
  store.sendError = null;
  // 페이지네이션 상태 초기화 + (polling 모드면) 새 방 커서 리셋. WS(프로젝트)는 유지.
  store.messagesCursor = null;
  store.messagesHasMore = false;
  store.loadingOlder = false;
  pollCursor = null;

  if (store.degraded) {
    store.messages = MOCK_MESSAGES[roomId] || [];
    markRoomReadLocal(roomId);
    return;
  }

  store.messagesLoading = true;
  store.messages = [];
  try {
    // 최초 20개(WG-CHAT-02). 더 과거는 위로 스크롤 시 loadOlderMessages 로 +20씩.
    const { messages, page } = await api.fetchMessages(roomId, { limit: 20 });
    store.messages = messages;
    store.messagesCursor = page?.next_cursor || null;
    store.messagesHasMore = !!page?.has_more;
    // 읽음 처리(WG-CHAT-03)
    const last = messages[messages.length - 1];
    api
      .markRead(roomId, { lastReadMessageId: last?.messageId })
      .then(() => markRoomReadLocal(roomId))
      .catch(() => {});
  } catch (e) {
    store.messages = [];
    store.sendError = e instanceof ApiError ? e.message : "메시지 로드 실패";
  } finally {
    store.messagesLoading = false;
  }
}

// 위로 스크롤 더보기: 현재 방의 더 과거 20개를 앞에 prepend(before-cursor).
export async function loadOlderMessages() {
  if (store.degraded) return; // mock 은 페이지네이션 없음
  if (!store.messagesHasMore || store.loadingOlder || !store.selectedRoomId) return;
  store.loadingOlder = true;
  const roomId = store.selectedRoomId;
  try {
    const { messages, page } = await api.fetchMessages(roomId, {
      limit: 20,
      cursor: store.messagesCursor,
    });
    if (store.selectedRoomId !== roomId) return; // 그 사이 방 전환되면 폐기
    const seen = new Set(store.messages.map((m) => m.messageId));
    const older = messages.filter((m) => !seen.has(m.messageId));
    store.messages = [...older, ...store.messages];
    store.messagesCursor = page?.next_cursor || null;
    store.messagesHasMore = !!page?.has_more;
  } catch {
    /* 실패 시 다음 시도 가능하도록 상태 유지 */
  } finally {
    store.loadingOlder = false;
  }
}

function markRoomReadLocal(roomId) {
  const r = store.rooms.find((x) => x.roomId === roomId);
  if (r) r.unread = 0;
}

export async function send() {
  const text = store.draft.trim();
  if (!text || store.sending) return;
  if (!canCompose()) return; // PM 방 외 송신 차단(이중 방어)

  const clientMessageId = nextClientId();
  // 낙관적 추가
  const optimistic = {
    messageId: clientMessageId,
    roomId: store.selectedRoomId,
    role: "PM",
    direction: "outbound",
    source: "webgui",
    messageType: "user_message",
    text,
    status: "pending",
    out: true,
    pending: true,
    occurredAt: new Date().toISOString(),
    // provenance(DS-60 §6.1): UI 발신 → SENT
    provLabel: "SENT",
    provTone: "sent",
    isRealData: true,
    isMock: false,
    teamSessionId: null,
  };
  store.messages.push(optimistic);
  store.draft = "";
  store.sending = true;
  store.sendError = null;

  if (store.degraded) {
    // 목업: pending → sent 로 표시
    setTimeout(() => {
      const m = store.messages.find((x) => x.messageId === clientMessageId);
      if (m) {
        m.status = "sent";
        m.pending = false;
      }
      store.sending = false;
    }, 250);
    return;
  }

  try {
    const data = await api.sendMessage({
      projectId: store.selectedProjectId,
      text,
      clientMessageId,
    });
    // 서버 message 로 낙관적 항목 치환 (전체 필드 갱신, persona §3.4)
    const saved = data?.message ? adaptMessage(data.message) : null;
    const idx = store.messages.findIndex((x) => x.messageId === clientMessageId);
    if (saved && idx >= 0) store.messages[idx] = saved;
    else if (saved) store.messages.push(saved);
  } catch (e) {
    const idx = store.messages.findIndex((x) => x.messageId === clientMessageId);
    if (idx >= 0) {
      store.messages[idx].status = "failed";
      store.messages[idx].pending = false;
      store.messages[idx].failed = true;
    }
    store.sendError = e instanceof ApiError ? e.message : "송신 실패";
  } finally {
    store.sending = false;
  }
}

// ── 산출물 트리 ─────────────────────────────────────────────
export async function loadTreeRoot() {
  if (store.degraded) {
    store.treeRoot = MOCK_TREE_ROOT;
    return;
  }
  store.treeLoading = true;
  try {
    const { node } = await api.fetchTree("", { depth: 1, projectId: store.selectedProjectId });
    store.treeRoot = node;
  } catch (e) {
    store.treeRoot = null;
  } finally {
    store.treeLoading = false;
  }
}

export async function toggleFolder(node) {
  const path = node.path;
  if (store.expanded[path]) {
    store.expanded[path] = false;
    return;
  }
  store.expanded[path] = true;
  // 이미 children 보유 or 캐시면 재요청 안 함
  if ((node.children && node.children.length) || store.childrenCache[path]) return;
  if (store.degraded) {
    store.childrenCache[path] = node.children || [];
    return;
  }
  store.childrenLoading[path] = true;
  try {
    const { node: loaded } = await api.fetchTree(path, { depth: 1, projectId: store.selectedProjectId });
    store.childrenCache[path] = loaded.children || [];
  } catch (e) {
    store.childrenCache[path] = [];
  } finally {
    store.childrenLoading[path] = false;
  }
}

export function childrenOf(node) {
  if (store.childrenCache[node.path]) return store.childrenCache[node.path];
  return node.children || [];
}

// ── 뷰어 ────────────────────────────────────────────────────
export async function openFile(node) {
  store.viewer.open = true;
  store.viewer.loading = true;
  store.viewer.error = null;
  store.viewer.path = node.path;
  store.viewer.file = null;

  if (store.degraded) {
    const f = MOCK_FILE[node.path];
    store.viewer.file = f || {
      path: node.path,
      name: node.name,
      ext: node.ext,
      renderMode: "unsupported",
      warnings: ["offline_mock"],
    };
    store.viewer.loading = false;
    return;
  }

  try {
    const file = await api.fetchFile(node.path, { prefer: "inline", projectId: store.selectedProjectId });
    store.viewer.file = file;
  } catch (e) {
    store.viewer.error = e instanceof ApiError ? e.message : "파일 열기 실패";
  } finally {
    store.viewer.loading = false;
  }
}

export function closeViewer() {
  store.viewer.open = false;
  store.viewer.file = null;
  store.viewer.error = null;
  store.viewer.path = null;
}

export function teardown() {
  stopRealtime();
}
