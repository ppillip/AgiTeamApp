// WebGUI API 엔드포인트 (DS-40 §5 목록). 모든 호출은 봉투 해제된 data 를 반환.
import { http, apiUrl, wsUrl, mediaUrl, uploadMultipart } from "./client.js";
import {
  adaptProjects,
  adaptRooms,
  adaptMessages,
  adaptNode,
  adaptFile,
  adaptAttachment,
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
// attachments: WG-MSG-06 으로 사전 업로드한 [{ attachment_id }] (순서 보존). 없으면 미포함.
export async function sendMessage({ projectId, text, clientMessageId, attachments }) {
  const body = {
    project_id: projectId,
    text,
    client_message_id: clientMessageId,
  };
  if (Array.isArray(attachments) && attachments.length) body.attachments = attachments;
  const data = await http.post(`${P}/messages`, body);
  return data; // { ack, message }
}

// WG-MSG-06 — 웹 채팅 이미지 첨부 업로드(multipart). 성공 시 attachment 메타(+preview_url) 반환.
// client_attachment_id 로 FE optimistic 썸네일과 매칭. onProgress(0~1) 로 진행률 콜백.
export async function uploadImageAttachment({ projectId, file, clientAttachmentId, onProgress }) {
  const fd = new FormData();
  fd.append("project_id", projectId);
  if (clientAttachmentId) fd.append("client_attachment_id", clientAttachmentId);
  fd.append("file", file, file.name || "image.png");
  const data = await uploadMultipart(`${P}/message-attachments/images`, fd, { onProgress });
  return adaptAttachment(data.attachment || data);
}

// 말풍선/썸네일 src. preview_url 은 self-contained(DS-40 v0.22 / DV-90): BE 가 attachment_id 로
// project 를 전역 해소하므로 project_id 쿼리가 불필요하다. preview_url 을 그대로 사용(+토큰만 부착).
export function attachmentPreviewSrc(previewUrl) {
  if (!previewUrl) return null;
  return mediaUrl(previewUrl);
}

// WG-MSG-04 — polling fallback.
// project_id 를 반드시 실어 보낸다(A-F1: BE 가 /message-updates 에 project_id 필수화 —
// 멀티프로젝트 격리 방어). 누락 시 BE 가 거절한다. project_id 는 호출부가 활성 프로젝트
// 컨텍스트(store.selectedProjectId)에서 전달한다.
export async function fetchUpdates(roomId, after, projectId) {
  const data = await http.get(`${P}/message-updates`, {
    project_id: projectId || undefined,
    room_id: roomId,
    after,
  });
  return data; // { updates, next_cursor }
}

// WG-MSG-05 — WebSocket update channel URL
export function messageStreamUrl(projectId, roomId, after) {
  return wsUrl(`${P}/message-stream`, { project_id: projectId, room_id: roomId, after });
}

// WG-ART-01 — 트리(1단계, lazy). project_id 를 반드시 실어 선택 프로젝트의 documents 를 조회한다.
// (미전달 시 백엔드가 settings.project_id 기본값으로 fallback → 프로젝트 전환해도 같은 트리. QI-WG-024)
// rootType(documents|system): 산출물(documents) ↔ 코드(system) 탭 전환. 미지정 시 BE 기본=documents.
export async function fetchTree(path, { depth = 1, projectId, rootType } = {}) {
  const data = await http.get(`${P}/artifacts/tree`, {
    project_id: projectId || undefined,
    root_type: rootType || undefined,
    path: path || undefined,
    depth,
    include_files: true,
  });
  return { root: data.root, path: data.path, node: adaptNode(data.node) };
}

// WG-ART-02 — 파일 메타·내용 (선택 프로젝트 기준). rootType 으로 documents/system 구분.
export async function fetchFile(path, { prefer = "inline", projectId, rootType } = {}) {
  const data = await http.get(`${P}/artifacts/file`, {
    project_id: projectId || undefined,
    root_type: rootType || undefined,
    path,
    prefer,
  });
  return adaptFile(data.file);
}

// WG-ART-05 — 산출물 파일 쓰기(MD 에디터 저장). 불칸 계약: POST /api/webgui/artifacts/write
//   body: { project_id, path, content }. 성공 시 저장된 파일 메타(있으면)를 adaptFile 로 정규화해 반환.
//   백엔드가 file 을 안 돌려줘도 호출부가 낙관적으로 content 를 반영하므로 { file:null } 도 정상.
export async function writeFile(path, content, { projectId, rootType } = {}) {
  const data = await http.post(`${P}/artifacts/write`, {
    project_id: projectId || undefined,
    root_type: rootType || undefined,
    path,
    content,
  });
  return { file: data?.file ? adaptFile(data.file) : null, raw: data };
}

// WG-ART-03 — 스트림 URL (pdf iframe/embed 용, 선택 프로젝트 기준)
export function fileStreamUrl(path, variant = "original", projectId, rootType) {
  return apiUrl(`${P}/artifacts/file/stream`, {
    project_id: projectId || undefined,
    root_type: rootType || undefined,
    path,
    variant,
  });
}

// WG-ART-04 — 산출물 변경 polling fallback (DS-40 §20). WebSocket(artifact_changed) 단절 중
// 산출물 폴더 변경을 화면에 반영하기 위한 보강 경로. 반환 모델은 artifact_changed 의 data 와 동일.
//   - after: 마지막 처리 cursor(`timestamp|artifact:<urlencoded path>`). 없으면 최근 변경 일부.
//   - cursor 만료 시 BE 는 409(artifact_change_cursor_expired) → 호출부가 full resync.
export async function fetchArtifactChanges(after, projectId, { limit = 200 } = {}) {
  const data = await http.get(`${P}/artifacts/changes`, {
    project_id: projectId || undefined,
    after: after || undefined,
    limit,
  });
  return data; // { updates, next_cursor }
}
