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
import { planArtifactChange } from "./artifactChange.js";
import { validateImageFile, MAX_ATTACH_COUNT } from "../lib/imageAttach.js";
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
  // 입력창 이미지 첨부(DV-91): pending 첨부 목록(순서 = 첨부 순서).
  //   { clientId, name, sizeBytes, mime, status('uploading'|'ready'|'error'), progress(0~100),
  //     localUrl(blob 미리보기), attachmentId, previewUrl, width, height, error }
  composerAttachments: [],
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

  // 외부 수정 표식 (WG-ART-06, DV-72): artifact_watcher 가 감지한 '외부' 변경 파일 path 집합.
  //   트리에서 해당 파일명을 amber 로 강조하고, 파일을 열면(openFile) 해제한다.
  //   자기 저장(saveArtifact)으로 인한 watcher echo 는 강조 대상에서 제외한다.
  externalChanges: {}, // path -> true
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
let roomsPollTimer = null; // 방 목록/연결상태 + 팀뷰 미리보기 polling
let artifactPollTimer = null; // 산출물 변경 polling(WG-ART-04 fallback)
let pollCursor = null;
// 산출물 변경 cursor(WG-ART-04). WS artifact_changed 의 envelope.cursor 로 갱신되며,
// WS 단절 시 polling 의 after 로 이어받아 누락 변경을 복구한다. WS↔polling 전환에는 보존하고
// 프로젝트 전환·정상종료(stopRealtime)에서만 초기화한다.
let artifactCursor = null;
let wsProjectId = null;
let wsReconnectTimer = null; // WS 재연결 예약 타이머
let wsReconnectAttempt = 0; // 재연결 시도 횟수(지수 backoff 인덱스)
const WS_BACKOFF = [1000, 2000, 5000, 10000]; // 1s→2s→5s→10s(상한)

function clearReconnect() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (roomsPollTimer) {
    clearInterval(roomsPollTimer);
    roomsPollTimer = null;
  }
  if (artifactPollTimer) {
    clearInterval(artifactPollTimer);
    artifactPollTimer = null;
  }
  pollCursor = null;
  // artifactCursor 는 여기서 비우지 않는다(WS↔polling 전환 간 누락 복구 위해 보존).
}

function stopRealtime() {
  clearReconnect();
  wsReconnectAttempt = 0;
  if (ws) {
    try {
      ws.close(1000);
    } catch {}
    ws = null;
  }
  stopPolling();
  artifactCursor = null; // 프로젝트 전환/정상종료 — 산출물 커서 완전 초기화
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
  // project_discovered 는 전역(새 프로젝트 발견) — 스코프 가드 예외(필터 전에 처리).
  if (type === "project_discovered") return upsertProjectEvent(data.project || data);
  // QI-WG-030(FE측 정합): project 스코프 가드(이중 방어). WS 는 project_id 단위로 열지만,
  // 이벤트에 project_id 가 실려 있고 현재 선택 프로젝트와 다르면 무시한다(프로젝트 전환 직후
  // 잔여 이벤트·혼선 방어). project_id 미동봉 이벤트는 room_id 기반 암묵 필터로 흡수(하위 함수).
  const evProjectId =
    env.project_id ?? data.project_id ?? (data.room && data.room.project_id) ?? null;
  if (evProjectId && store.selectedProjectId && evProjectId !== store.selectedProjectId) return;
  // 산출물 변경(DV-71, DS-40 §10.3): 같은 message-stream 채널로 수신. envelope.cursor 를
  // 다음 polling fallback 의 after 로 보존하고, 열린 트리/현재 뷰어 파일만 재요청한다.
  if (type === "artifact_changed") {
    if (env.cursor) artifactCursor = env.cursor;
    return applyArtifactChange(data);
  }
  if (type === "room_upserted") return upsertRoomEvent(data.room || data);
  if (type === "room_connection_changed") return applyRoomConnection(data.room || data);
  // 런타임 활동 변경(요구사항 15-1): 연결상태(liveness) 위에 얹는 2차원(active/idle).
  if (type === "runtime_activity_changed") return applyRuntimeActivity(data.room || data);
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

// 런타임 활동 변경(요구사항 15-1, 아테나 인터페이스): runtime_activity_changed WS 이벤트 1건을
// 방에 반영한다. payload: { project_id, role, runtime_activity(active|idle|unknown),
//   from_activity, ts, reason, offset_start/end, chunk_bytes, ... } — 카드 표시엔 runtime_activity 만 사용.
// 매칭: role + project_id(+ room_id). 연결상태와 독립 차원이므로 runtimeActivity 만 갱신한다.
function applyRuntimeActivity(raw) {
  if (!raw) return;
  // 스코프 가드(이중 방어): 타 프로젝트 이벤트 무시(handleWsEvent 에서 1차 필터되나 안전망).
  if (raw.project_id && store.selectedProjectId && raw.project_id !== store.selectedProjectId) return;
  const r = store.rooms.find(
    (x) => x.roomId === raw.room_id || (x.projectId === raw.project_id && x.role === raw.role)
  );
  if (!r) return;
  if (raw.runtime_activity) r.runtimeActivity = raw.runtime_activity; // active | idle | unknown
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
  if (pollTimer) return; // 이미 폴링 중(재연결 실패 반복 시 중복 타이머 방지)
  pollCursor = null;
  // 선택방 본문 스레드(단일방 보기)
  pollTimer = setInterval(async () => {
    const rid = store.selectedRoomId;
    if (!rid) return;
    try {
      // A-F1: project_id 를 함께 전달(BE 필수화). 활성 프로젝트 컨텍스트 기준.
      const { updates, next_cursor } = await api.fetchUpdates(rid, pollCursor, store.selectedProjectId);
      if (next_cursor) pollCursor = next_cursor;
      applyUpdates(updates);
    } catch {
      /* 다음 tick 재시도 */
    }
  }, 3000);
  // 방 목록/연결상태 + 전체 방 미리보기(팀뷰). WS 가 죽어도 팀뷰가 멈추지 않게 폴백 커버.
  roomsPollTimer = setInterval(async () => {
    if (store.selectedProjectId !== projectId) return;
    try {
      mergeRooms(await api.fetchRooms(projectId));
    } catch {}
    try {
      await loadRoomPreviews(6, { silent: true }); // 전체 팀원 보기 미리보기 갱신(스피너 토글 없이)
    } catch {}
  }, 5000);
  // 산출물 변경 polling(WG-ART-04 fallback). WS 단절 동안 열린 트리·뷰어 파일을 동일 매핑으로 갱신.
  //   artifactCursor 를 after 로 이어받아 누락분만 수신하고, 만료(409) 시 full resync 한다.
  artifactPollTimer = setInterval(async () => {
    if (store.selectedProjectId !== projectId || store.degraded) return;
    try {
      const { updates, next_cursor } = await api.fetchArtifactChanges(artifactCursor, projectId);
      if (next_cursor) artifactCursor = next_cursor;
      if (Array.isArray(updates)) for (const u of updates) applyArtifactChange(u);
    } catch (e) {
      // cursor 만료/buffer 밖 → 열린 트리 root + 뷰어 파일 full resync(DS-40 §20.3, DS-60 §8.4)
      if (e instanceof ApiError && (e.status === 409 || e.code === "artifact_change_cursor_expired")) {
        artifactCursor = null;
        try {
          await fullArtifactResync();
        } catch {}
      }
      /* 그 외 일시 오류는 다음 tick 재시도 */
    }
  }, 4000);
}

function startRealtime(projectId) {
  stopRealtime();
  if (store.degraded || !projectId) return;
  wsProjectId = projectId;
  wsReconnectAttempt = 0;
  connectWs(projectId);
}

// WS 끊김 시 지수 backoff 로 재연결 예약(폴링은 그동안 폴백으로 유지).
// 프로젝트 전환·정상종료 시엔 예약하지 않는다.
function scheduleReconnect(projectId) {
  if (store.selectedProjectId !== projectId || store.degraded) return;
  if (wsReconnectTimer) return; // 이미 예약됨
  const delay = WS_BACKOFF[Math.min(wsReconnectAttempt, WS_BACKOFF.length - 1)];
  wsReconnectAttempt++;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    if (store.selectedProjectId !== projectId || store.degraded) return;
    connectWs(projectId); // WS 만 재시도(폴링 타이머는 유지)
  }, delay);
}

// 프로젝트 단위 WebSocket 연결(room_id 없이 프로젝트 전역 이벤트 구독).
// onopen: backoff 리셋 + 폴링 중지(WS 우선). onclose(비정상): 즉시 폴백 + 재연결 예약.
//
// QI-WG-030 gap replay: 재연결 동안의 누락분은 onclose→startPolling 의 cursor 폴백이 메우고,
// applyUpdates 가 message_id 로 dedup 하여 중복 말풍선을 막는다(무손실). WS 자체 after replay 는
// messageStreamUrl(projectId, roomId, after) 슬롯이 준비돼 있으나, BE message-stream 의 after
// replay 계약(after 의미=cursor/occurred_at)이 확정되면 connectWs(projectId, after) 로 적용한다.
function connectWs(projectId) {
  try {
    const url = api.messageStreamUrl(projectId);
    ws = new WebSocket(url);
    ws.onopen = () => {
      if (store.selectedProjectId !== projectId) {
        try { ws && ws.close(1000); } catch {}
        return;
      }
      wsReconnectAttempt = 0; // 연결 성공 → backoff 리셋
      clearReconnect();
      stopPolling(); // WS 우선 → 폴백 폴링 중지
    };
    ws.onmessage = handleWsEvent;
    ws.onerror = () => {
      try {
        ws && ws.close();
      } catch {}
      // 이어서 onclose 가 호출됨 → 거기서 폴백/재연결 일괄 처리
    };
    ws.onclose = (e) => {
      ws = null;
      if (store.selectedProjectId !== projectId) return; // 프로젝트 전환됨
      if (e && e.code === 1000) return; // 정상 종료(stopRealtime 등)
      startPolling(projectId); // 즉시 폴백(팀뷰·단일방 안 멈춤)
      scheduleReconnect(projectId); // WS 재연결 시도
    };
  } catch {
    if (store.selectedProjectId === projectId) {
      startPolling(projectId);
      scheduleReconnect(projectId);
    }
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
    if (v === undefined || v === null) continue;
    // 첨부(이미지)는 희소 WS 갱신의 빈 배열이 기존 썸네일을 지우지 않도록 보호:
    // next.attachments 가 비어 있고 prev 에 첨부가 있으면 prev 를 유지한다(DV-91).
    if (k === "attachments" && Array.isArray(v) && v.length === 0 && Array.isArray(prev.attachments) && prev.attachments.length) {
      continue;
    }
    merged[k] = v;
  }
  return merged;
}

// 서버 message 로 낙관적 항목을 치환하되 첨부 썸네일 연속성을 보존(DV-91).
//  - 서버가 attachment 를 안 돌려주면 낙관적 첨부(localUrl 포함) 유지.
//  - 서버가 attachment 를 주면 그것을 쓰되, 같은 attachment_id 의 낙관적 localUrl 을 이식해
//    서버 preview 준비 전 깜빡임을 줄인다(localUrl 은 송신 성공 시 별도 revoke).
function preserveAttachments(prev, saved) {
  const prevAtt = (prev && Array.isArray(prev.attachments) && prev.attachments) || [];
  if (!Array.isArray(saved.attachments) || saved.attachments.length === 0) {
    if (prevAtt.length) saved.attachments = prevAtt;
    return saved;
  }
  saved.attachments = saved.attachments.map((a) => {
    const m = prevAtt.find((p) => p.attachmentId && p.attachmentId === a.attachmentId);
    return m && m.localUrl && !a.localUrl ? { ...a, localUrl: m.localUrl } : a;
  });
  return saved;
}

// update(MessageUpdate) 목록을 머지. 선택된 방은 본문 스레드(store.messages)에,
// 그리고 전체 팀원 보기를 위해 해당 방의 미리보기(roomPreviews)에도 실시간 반영한다.
function applyUpdates(updates) {
  if (!Array.isArray(updates)) return;
  for (const u of updates) {
    if (!u || !u.message) continue; // 이벤트성(correlation_closed 등)은 본문 없음 → skip
    const rawId = u.message.message_id;
    // 낙관적 말풍선 상관 키(BE 가 broadcast 에 추가하는 client_message_id).
    // 서버 id 로 매칭 실패 시, send() 가 만든 pending 항목(messageId=clientMessageId)을
    // 이 키로 찾아 치환/머지하여 말풍선 중복(race) 을 제거한다.
    const clientId = u.message.client_message_id;
    // 1) 선택된 방의 본문 스레드 갱신
    if (u.room_id === store.selectedRoomId) {
      let idx = rawId != null ? store.messages.findIndex((x) => x.messageId === rawId) : -1;
      if (idx < 0 && clientId != null)
        idx = store.messages.findIndex((x) => x.pending && x.messageId === clientId);
      const existing = idx >= 0 ? store.messages[idx] : null;
      const m = adaptMessage(enrichUpdateMessage(u.message, u.update_type, u.occurred_at, existing));
      if (idx >= 0) store.messages[idx] = mergeMessage(store.messages[idx], m); // 낙관적→서버 치환(중복 방지)
      else store.messages.push(m); // 신규 말풍선 → 즉시 추가(실시간 렌더)
    }
    // 2) 미리보기 갱신(전체 팀원 보기 — 선택 여부 무관, 모든 방)
    let pv = store.roomPreviews[u.room_id];
    // 신규 방(아직 미리보기 엔트리 없음)도 팀뷰에 실시간 반영(C). 알려진 방만 생성.
    if (!pv && store.rooms.some((r) => r.roomId === u.room_id)) {
      pv = store.roomPreviews[u.room_id] = [];
    }
    if (pv) {
      let pi = rawId != null ? pv.findIndex((x) => x.messageId === rawId) : -1;
      if (pi < 0 && clientId != null)
        pi = pv.findIndex((x) => x.pending && x.messageId === clientId);
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
export async function loadRoomPreviews(limit = 6, { silent = false } = {}) {
  const rooms = store.rooms.slice();
  if (!rooms.length) return;
  // silent: 폴링 폴백의 주기 갱신 — 스피너 토글 없이 조용히 갱신(깜빡임 방지)
  if (!silent) store.previewsLoading = true;
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
          // silent 갱신 중 실패는 기존 미리보기 보존(빈 배열로 덮어 깜빡이지 않게)
          if (!silent) store.roomPreviews[r.roomId] = [];
        }
      })
    );
  } finally {
    if (!silent) store.previewsLoading = false;
  }
}

// ── 산출물 실시간 갱신 (DV-71, DS-40 §10.3/§10.4, DS-60 §8.4) ──────────
// artifact_changed(WS) 또는 WG-ART-04 polling 의 변경 1건을 화면에 반영한다.
// 매핑 "결정"은 순수 모듈 planArtifactChange 가, 실제 부수효과(REST 재요청·상태 갱신)는 여기서 담당.
// 자기 저장(saveArtifact) echo 억제: 우리가 방금 쓴 파일은 watcher 가 곧 modified 로 알려오는데,
// 이는 '외부' 변경이 아니므로 트리 강조 대상에서 뺀다. 저장 직전 path 를 잠깐(TTL) 등록해 두고
// 그 사이 들어온 같은 path 의 변경은 강조하지 않는다.
const SELF_WRITE_TTL = 10000; // ms — watcher 디바운스 + 네트워크 지연 여유
const recentSelfWrites = new Map(); // path -> timeoutId
function noteSelfWrite(path) {
  if (!path) return;
  const prev = recentSelfWrites.get(path);
  if (prev) clearTimeout(prev);
  recentSelfWrites.set(
    path,
    setTimeout(() => recentSelfWrites.delete(path), SELF_WRITE_TTL)
  );
}
function isSelfWrite(path) {
  return recentSelfWrites.has(path);
}

// 외부 변경 표식 추가/해제 (WG-ART-06). 자기 저장 echo·현재 보고 있는 파일은 강조하지 않는다.
function markExternalChange(path) {
  if (!path) return;
  if (isSelfWrite(path)) return; // 자기 저장 echo → 외부 변경 아님
  if (store.viewer.open && store.viewer.path === path) return; // 보고 있는 파일 → 자동 reload 로 충분
  store.externalChanges[path] = true;
}
export function clearExternalChange(path) {
  if (path && store.externalChanges[path]) delete store.externalChanges[path];
}

function applyArtifactChange(data) {
  if (store.degraded) return;
  const plan = planArtifactChange(data, {
    selectedProjectId: store.selectedProjectId,
    viewerOpen: store.viewer.open,
    viewerPath: store.viewer.path,
    expanded: store.expanded,
  });
  if (plan.ignore) return;

  // 0) 외부 수정 표식(WG-ART-06): created/modified 는 트리에서 파일명 강조 대상.
  //    deleted 는 노드 자체가 사라지므로 강조 불필요.
  if (plan.changeType === "modified" || plan.changeType === "created") {
    markExternalChange(plan.path);
  }

  // 1) 현재 뷰어 중인 파일 변경 반영
  if (plan.viewer === "deleted") {
    store.viewer.file = null;
    store.viewer.error = "삭제된 산출물입니다 — 더 이상 존재하지 않습니다.";
  } else if (plan.viewer === "reload") {
    reloadViewer(plan.path); // modified/created → 최신 내용 재로딩
  }

  // 2) 삭제된 디렉토리면 트리 보조 상태(펼침/자식 캐시) 잔여 정리
  if (plan.purge) purgeSubtree(plan.path);

  // 3) 트리 노드 동기화(13-3): 변경 노드를 나열하는 디렉토리 갱신.
  //    - refreshDir(보임): 그 디렉토리만 즉시 재요청 — created 새 노드/deleted 제거 즉시 반영.
  //    - staleDir(접힘+구성변경): stale 자식 캐시 무효화 → 다음 펼침(toggleFolder)에서 최신 로드.
  //      (이 무효화가 없으면 toggleFolder 가 stale 캐시로 재요청을 건너뛰어 새 파일이 영영 안 보임)
  if (plan.refreshDir !== null) refreshDirIfVisible(plan.refreshDir);
  else if (plan.staleDir) invalidateDirCache(plan.staleDir);
}

// 접힌 디렉토리의 stale 자식 캐시를 비워, 다음 펼침에서 fetchTree 로 재조회되게 한다.
// (created/deleted 로 children 구성이 바뀌었으나 화면에 보이지 않아 즉시 재조회하지 않는 경우)
function invalidateDirCache(dirPath) {
  if (!dirPath) return;
  if (store.childrenCache[dirPath]) delete store.childrenCache[dirPath];
}

// 펼침/자식 캐시에서 path 및 그 하위 경로 키를 제거(삭제된 디렉토리 잔여 정리)
function purgeSubtree(path) {
  const pref = path + "/";
  for (const k of Object.keys(store.expanded)) {
    if (k === path || k.startsWith(pref)) delete store.expanded[k];
  }
  for (const k of Object.keys(store.childrenCache)) {
    if (k === path || k.startsWith(pref)) delete store.childrenCache[k];
  }
}

// 디렉토리가 화면에 보이는 경우에만 WG-ART-01 로 재조회(루트는 항상 보임).
// 보이지 않으면 재요청하지 않는다(DS-40 §10.4: 다음 펼침 때 최신 결과 사용).
async function refreshDirIfVisible(dirPath) {
  if (store.degraded) return;
  const pid = store.selectedProjectId;
  const isRoot = dirPath === "" || dirPath == null;
  if (!isRoot && !store.expanded[dirPath]) return; // 미펼침 → skip
  try {
    const { node } = await api.fetchTree(isRoot ? "" : dirPath, { depth: 1, projectId: pid });
    if (store.selectedProjectId !== pid) return; // 그 사이 프로젝트 전환 → 폐기
    if (isRoot) store.treeRoot = node;
    else store.childrenCache[dirPath] = node.children || [];
  } catch {
    /* 일시 실패는 다음 이벤트/폴링에서 복구 */
  }
}

// 현재 뷰어 파일 내용 재로딩(modified 반영). 비동기 도중 다른 파일로 전환되면 폐기.
async function reloadViewer(path) {
  if (store.degraded) return;
  const pid = store.selectedProjectId;
  try {
    const file = await api.fetchFile(path, { prefer: "inline", projectId: pid });
    if (store.viewer.open && store.viewer.path === path && store.selectedProjectId === pid) {
      store.viewer.file = file;
      store.viewer.error = null;
    }
  } catch (e) {
    if (store.viewer.open && store.viewer.path === path && store.selectedProjectId === pid) {
      // 재로딩 중 사라졌다면(404 등) not-found 안내로 전환
      store.viewer.error = e instanceof ApiError ? e.message : "파일 재로딩 실패";
    }
  }
}

// cursor 만료(409) 시 full resync(DS-60 §8.4): 열린 트리 root + 펼친 디렉토리 + 현재 뷰어 파일.
async function fullArtifactResync() {
  if (store.degraded) return;
  await refreshDirIfVisible("");
  for (const p of Object.keys(store.expanded)) {
    if (store.expanded[p]) await refreshDirIfVisible(p);
  }
  if (store.viewer.open && store.viewer.path) await reloadViewer(store.viewer.path);
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
  clearComposerAttachments(); // 입력창 pending 이미지 첨부 정리(이전 프로젝트 컨텍스트)
  store.sendError = null;
  // 산출물 트리 초기화 후 재로드
  store.treeRoot = null;
  store.expanded = {};
  store.childrenCache = {};
  store.externalChanges = {}; // 외부 수정 표식 초기화(이전 프로젝트 컨텍스트)
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

// ── 입력창 이미지 첨부 (DV-91, WG-MSG-06) ──────────────────────
let attachSeq = 0;
const nextAttachId = () => `client_att_${Date.now()}_${++attachSeq}`;

// paste/드롭/파일선택으로 들어온 파일들을 검증·업로드한다(순서 보존). PM 방에서만 허용.
export function addComposerImages(files) {
  if (!canCompose()) return;
  const list = Array.from(files || []).filter(Boolean);
  store.sendError = null;
  for (const file of list) {
    // 메시지당 개수 한도(5개) — 한도 도달 시 나머지 무시 + 안내
    if (store.composerAttachments.length >= MAX_ATTACH_COUNT) {
      store.sendError = `이미지는 메시지당 최대 ${MAX_ATTACH_COUNT}개까지 첨부할 수 있습니다.`;
      break;
    }
    const clientId = nextAttachId();
    const v = validateImageFile(file);
    if (!v.ok) {
      // 형식/용량 위반 → error 칩으로 표시(전송 차단). 사용자가 제거 후 전송.
      store.composerAttachments.push({
        clientId, name: file.name || "image", sizeBytes: file.size, mime: file.type,
        status: "error", progress: 0, localUrl: null, attachmentId: null, previewUrl: null,
        width: null, height: null, error: v.message,
      });
      store.sendError = v.message;
      continue;
    }
    const localUrl = typeof URL !== "undefined" && URL.createObjectURL ? URL.createObjectURL(file) : null;
    const entry = {
      clientId, name: file.name || "image", sizeBytes: file.size, mime: file.type,
      status: "uploading", progress: 0, localUrl, attachmentId: null, previewUrl: null,
      width: null, height: null, error: null,
    };
    store.composerAttachments.push(entry);
    if (store.degraded) {
      // 백엔드 미연결 → 실제 업로드 불가. 미리보기만 두되 전송 불가(error) 처리.
      entry.status = "error";
      entry.error = "오프라인 상태에서는 업로드할 수 없습니다.";
      continue;
    }
    uploadComposerImage(entry, file);
  }
}

async function uploadComposerImage(entry, file) {
  const pid = store.selectedProjectId;
  try {
    const att = await api.uploadImageAttachment({
      projectId: pid,
      file,
      clientAttachmentId: entry.clientId,
      onProgress: (p) => {
        const live = store.composerAttachments.find((a) => a.clientId === entry.clientId);
        if (live) live.progress = Math.round(p * 100);
      },
    });
    const live = store.composerAttachments.find((a) => a.clientId === entry.clientId);
    if (!live) return; // 업로드 중 사용자가 제거 → 폐기
    live.attachmentId = att.attachmentId;
    live.previewUrl = att.previewUrl;
    live.width = att.width;
    live.height = att.height;
    if (att.sizeBytes != null) live.sizeBytes = att.sizeBytes;
    live.progress = 100;
    live.status = "ready";
  } catch (e) {
    const live = store.composerAttachments.find((a) => a.clientId === entry.clientId);
    if (!live) return;
    live.status = "error";
    live.error = e instanceof ApiError ? e.message : "업로드 실패";
    store.sendError = "이미지 업로드에 실패했습니다. 해당 이미지를 제거 후 다시 시도하세요.";
  }
}

export function removeComposerAttachment(clientId) {
  const i = store.composerAttachments.findIndex((a) => a.clientId === clientId);
  if (i < 0) return;
  const a = store.composerAttachments[i];
  if (a.localUrl) {
    try { URL.revokeObjectURL(a.localUrl); } catch {}
  }
  store.composerAttachments.splice(i, 1);
  if (!store.composerAttachments.some((x) => x.status === "error")) store.sendError = null;
}

function clearComposerAttachments({ revoke = true } = {}) {
  if (revoke) {
    for (const a of store.composerAttachments) {
      if (a.localUrl) {
        try { URL.revokeObjectURL(a.localUrl); } catch {}
      }
    }
  }
  store.composerAttachments = [];
}

export function composerUploading() {
  return store.composerAttachments.some((a) => a.status === "uploading");
}
export function composerHasError() {
  return store.composerAttachments.some((a) => a.status === "error");
}
export function composerReadyAttachments() {
  return store.composerAttachments.filter((a) => a.status === "ready");
}
// 전송 가능 여부: (텍스트 또는 준비된 첨부) 있고, 업로드 중/실패 첨부가 없을 것.
export function canSend() {
  if (store.sending || !canCompose()) return false;
  const hasText = store.draft.trim().length > 0;
  const hasReady = composerReadyAttachments().length > 0;
  if (!hasText && !hasReady) return false;
  if (composerUploading() || composerHasError()) return false;
  return true;
}

export async function send() {
  const text = store.draft.trim();
  if (store.sending) return;
  if (!canCompose()) return; // PM 방 외 송신 차단(이중 방어)

  const ready = composerReadyAttachments();
  if (!text && ready.length === 0) return; // 빈 메시지(텍스트·첨부 모두 없음) 차단
  // 일부 실패 시 전체 실패(DS-40 §7.6): 업로드 중·실패 첨부가 있으면 전송 차단
  if (composerUploading()) {
    store.sendError = "이미지 업로드가 끝날 때까지 기다려 주세요.";
    return;
  }
  if (composerHasError()) {
    store.sendError = "업로드 실패한 이미지를 제거한 뒤 전송하세요.";
    return;
  }

  const clientMessageId = nextClientId();
  // 송신 payload: attachment_id 순서 보존
  const attachPayload = ready.map((a) => ({ attachment_id: a.attachmentId }));
  // 낙관적 말풍선용 첨부(로컬 blob 미리보기 우선, 서버 preview_url 병행) — adaptAttachment 와 동일 카멜 형태
  const optimisticAttachments = ready.map((a) => ({
    attachmentId: a.attachmentId,
    previewUrl: a.previewUrl,
    localUrl: a.localUrl,
    name: a.name,
    mimeType: a.mime,
    width: a.width,
    height: a.height,
    sizeBytes: a.sizeBytes,
    kind: "image",
  }));
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
    attachments: optimisticAttachments,
  };
  store.messages.push(optimistic);
  store.draft = "";
  // composer 썸네일은 비운다. localUrl 은 말풍선(optimistic)이 계속 참조하므로 여기서 revoke 금지.
  const sentLocalUrls = ready.map((a) => a.localUrl).filter(Boolean);
  clearComposerAttachments({ revoke: false });
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
      attachments: attachPayload,
    });
    // 서버 message 로 낙관적 항목 치환 (전체 필드 갱신, persona §3.4)
    const saved = data?.message ? adaptMessage(data.message) : null;
    // 서버가 attachment(preview_url)를 돌려줬는지 — 줬으면 서버 preview 로 전환, 아니면 낙관적 유지
    const serverHasAttachments = !!(saved && Array.isArray(saved.attachments) && saved.attachments.length);
    const idx = store.messages.findIndex((x) => x.messageId === clientMessageId);
    if (saved && idx >= 0) store.messages[idx] = preserveAttachments(store.messages[idx], saved);
    else if (saved) {
      // 낙관적 항목이 안 보이면, 이미 WS 이벤트가 같은 서버 메시지를 반영(치환)했을 수 있다.
      // 같은 messageId 가 이미 있으면 push 금지 → 머지(중복 말풍선 방지, race).
      const dup = store.messages.findIndex((x) => x.messageId === saved.messageId);
      if (dup >= 0) store.messages[dup] = mergeMessage(store.messages[dup], saved);
      else store.messages.push(saved);
    }
    // 서버 preview 로 전환된 경우에만 로컬 blob URL 해제(누수 방지).
    // 서버가 attachment 를 안 돌려줬으면 낙관적 localUrl 을 계속 써야 하므로 유지한다.
    if (serverHasAttachments) {
      for (const u of sentLocalUrls) {
        try { URL.revokeObjectURL(u); } catch {}
      }
    }
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
  // 파일을 여는 순간 외부 수정 표식 해제(WG-ART-06: '열어서 보는 순간 색상 원복')
  clearExternalChange(node.path);
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

// ── 산출물 파일 쓰기 (MD 에디터 저장, WG-ART-05) ───────────────
// 백엔드 /artifacts/write 호출 → 저장 성공 시 현재 뷰어 파일 내용을 최신화(낙관적).
// 자기 저장은 watcher echo 로 외부 수정 강조되지 않게 noteSelfWrite 로 미리 등록한다.
export async function saveArtifact(path, content) {
  if (!path) return null;
  if (store.degraded) {
    throw new ApiError("오프라인 상태에서는 저장할 수 없습니다.", { code: "offline_save" });
  }
  const pid = store.selectedProjectId;
  noteSelfWrite(path); // 저장 직전 등록(이후 watcher 의 같은 path 변경은 외부 강조 제외)
  const { file } = await api.writeFile(path, content, { projectId: pid });
  // 저장 성공 → 현재 뷰어 파일 즉시 최신화. 서버가 파일 메타를 주면 그것으로, 아니면 content 만 반영.
  if (store.viewer.open && store.viewer.path === path && store.selectedProjectId === pid) {
    if (file) store.viewer.file = file;
    else if (store.viewer.file) store.viewer.file = { ...store.viewer.file, content };
  }
  clearExternalChange(path); // 자기 저장본은 외부 변경 아님 → 표식 해제
  return file;
}

export function teardown() {
  stopRealtime();
}
