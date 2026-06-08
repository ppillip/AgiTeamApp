// WebGUI API 엔드포인트 (DS-40 §5 목록). 모든 호출은 봉투 해제된 data 를 반환.
import { http, apiUrl, wsUrl } from "./client.js";
import {
  adaptProjects,
  adaptRooms,
  adaptMessages,
  adaptNode,
  adaptFile,
} from "./adapters.js";

const P = "/api/webgui";

// WG-PROJ-01
export async function fetchProjects() {
  const data = await http.get(`${P}/projects`);
  return adaptProjects(data);
}

// WG-CHAT-01
export async function fetchRooms(projectId) {
  const data = await http.get(`${P}/rooms`, { project_id: projectId });
  return adaptRooms(data);
}

// WG-CHAT-02 (기본 desc → 화면 표시용 asc 로 뒤집어 반환)
export async function fetchMessages(roomId, { limit = 50, cursor } = {}) {
  const data = await http.get(`${P}/rooms/${roomId}/messages`, {
    limit,
    cursor,
    direction: "desc",
  });
  const messages = adaptMessages(data.messages).reverse();
  return { messages, page: data.page || {}, room: data.room || null };
}

// WG-CHAT-03
export async function markRead(roomId, { readUntil, lastReadMessageId } = {}) {
  return http.post(`${P}/rooms/${roomId}/read`, {
    read_until: readUntil,
    last_read_message_id: lastReadMessageId,
  });
}

// WG-MSG-02 — PM 경유 송신 (project_id 만 필요)
export async function sendMessage({ projectId, text, clientMessageId }) {
  const data = await http.post(`${P}/messages`, {
    project_id: projectId,
    text,
    client_message_id: clientMessageId,
  });
  return data; // { ack, message }
}

// WG-MSG-04 — polling fallback
export async function fetchUpdates(roomId, after) {
  const data = await http.get(`${P}/message-updates`, { room_id: roomId, after });
  return data; // { updates, next_cursor }
}

// WG-MSG-05 — WebSocket update channel URL
export function messageStreamUrl(projectId, roomId, after) {
  return wsUrl(`${P}/message-stream`, { project_id: projectId, room_id: roomId, after });
}

// WG-ART-01 — 트리(1단계, lazy). project_id 를 반드시 실어 선택 프로젝트의 documents 를 조회한다.
// (미전달 시 백엔드가 settings.project_id 기본값으로 fallback → 프로젝트 전환해도 같은 트리. QI-WG-024)
export async function fetchTree(path, { depth = 1, projectId } = {}) {
  const data = await http.get(`${P}/artifacts/tree`, {
    project_id: projectId || undefined,
    path: path || undefined,
    depth,
    include_files: true,
  });
  return { root: data.root, path: data.path, node: adaptNode(data.node) };
}

// WG-ART-02 — 파일 메타·내용 (선택 프로젝트 기준)
export async function fetchFile(path, { prefer = "inline", projectId } = {}) {
  const data = await http.get(`${P}/artifacts/file`, { project_id: projectId || undefined, path, prefer });
  return adaptFile(data.file);
}

// WG-ART-03 — 스트림 URL (pdf iframe/embed 용, 선택 프로젝트 기준)
export function fileStreamUrl(path, variant = "original", projectId) {
  return apiUrl(`${P}/artifacts/file/stream`, { project_id: projectId || undefined, path, variant });
}
