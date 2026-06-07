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
import { adaptMessage } from "../api/adapters.js";
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

// ── 실시간 채널 ──────────────────────────────────────────────
let ws = null;
let pollTimer = null;
let pollCursor = null;

function stopRealtime() {
  if (ws) {
    try {
      ws.close();
    } catch {}
    ws = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  pollCursor = null;
}

function startPolling(projectId, roomId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (store.selectedRoomId !== roomId) return;
    try {
      const { updates, next_cursor } = await api.fetchUpdates(roomId, pollCursor);
      if (next_cursor) pollCursor = next_cursor;
      applyUpdates(updates);
    } catch {
      /* 폴링 실패는 조용히 무시(다음 tick 재시도) */
    }
  }, 3000);
}

function startRealtime(projectId, roomId) {
  stopRealtime();
  if (store.degraded) return;
  // WebSocket 우선
  try {
    const url = api.messageStreamUrl(projectId, roomId);
    ws = new WebSocket(url);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "message_update" && msg.data) applyUpdates([msg.data]);
      } catch {}
    };
    ws.onerror = () => {
      // WS 불가 → polling 폴백
      try {
        ws && ws.close();
      } catch {}
      ws = null;
      startPolling(projectId, roomId);
    };
    ws.onclose = (e) => {
      if (store.selectedRoomId === roomId && !pollTimer && e.code !== 1000) {
        startPolling(projectId, roomId);
      }
    };
  } catch {
    startPolling(projectId, roomId);
  }
}

// update(MessageUpdate) 목록을 현재 메시지에 머지 (방어적: messageId 기준)
function applyUpdates(updates) {
  if (!Array.isArray(updates)) return;
  for (const u of updates) {
    if (!u || u.room_id !== store.selectedRoomId) continue;
    if (u.message) {
      const m = adaptMessage(u.message);
      const idx = store.messages.findIndex((x) => x.messageId === m.messageId);
      if (idx >= 0) store.messages[idx] = { ...store.messages[idx], ...m };
      else store.messages.push(m);
    }
  }
}

// ── 액션 ────────────────────────────────────────────────────

export async function boot() {
  store.projectsLoading = true;
  store.bootError = null;
  try {
    const { projects, selectedProjectId } = await api.fetchProjects();
    if (!projects.length) throw new ApiError("프로젝트 없음", { code: "empty" });
    store.degraded = false;
    store.projects = projects;
    await selectProject(selectedProjectId || projects[0].projectId);
  } catch (e) {
    // 백엔드 미연결/빈 결과 → degraded 모드(mock)
    enterDegraded(e);
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
  loadTreeRoot();
}

export async function selectRoom(roomId) {
  if (!roomId) return;
  store.selectedRoomId = roomId;
  store.sendError = null;
  stopRealtime();

  if (store.degraded) {
    store.messages = MOCK_MESSAGES[roomId] || [];
    markRoomReadLocal(roomId);
    return;
  }

  store.messagesLoading = true;
  store.messages = [];
  try {
    const { messages } = await api.fetchMessages(roomId, { limit: 50 });
    store.messages = messages;
    // 읽음 처리(WG-CHAT-03)
    const last = messages[messages.length - 1];
    api
      .markRead(roomId, { lastReadMessageId: last?.messageId })
      .then(() => markRoomReadLocal(roomId))
      .catch(() => {});
    // 실시간 시작
    pollCursor = null;
    startRealtime(store.selectedProjectId, roomId);
  } catch (e) {
    store.messages = [];
    store.sendError = e instanceof ApiError ? e.message : "메시지 로드 실패";
  } finally {
    store.messagesLoading = false;
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
    const { node } = await api.fetchTree("", { depth: 1 });
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
    const { node: loaded } = await api.fetchTree(path, { depth: 1 });
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
    const file = await api.fetchFile(node.path, { prefer: "inline" });
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
