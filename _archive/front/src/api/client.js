// HTTP 클라이언트 — DS-40 공통 규칙(§3) 처리.
//  - Base URL: VITE_API_BASE (기본 '' = 동일 출처, vite dev proxy 가 /api → 백엔드 전달)
//  - 인증: VITE_API_TOKEN 있으면 Authorization: Bearer 헤더 (DS-40 §3.2). 로컬 dev 는 미설정 가능.
//  - 봉투 해제: 성공 {ok:true,data} → data 반환. 실패는 ApiError throw (code/message/status).
//  - DB 미가동 등 503 은 ApiError(status=503, code) 로 전달 → 상위에서 degraded 처리.

const BASE = (import.meta.env?.VITE_API_BASE ?? "").replace(/\/$/, "");
const TOKEN = import.meta.env?.VITE_API_TOKEN || null;
// WS 전용 베이스(예: ws://localhost:8000). 설정 시 WebSocket 을 백엔드로 직접 연결한다
// (dev 의 vite ws 프록시 우회 — DV-48). 미설정 시 동일 출처(프록시) — 프로덕션 동일출처 배포 호환.
const WS_BASE = (import.meta.env?.VITE_WS_BASE || "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message, { code = "error", status = 0, details = null } = {}) {
    super(message || code);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

function authHeaders(extra) {
  const h = { ...(extra || {}) };
  if (TOKEN) h["Authorization"] = `Bearer ${TOKEN}`;
  return h;
}

export function apiUrl(path, params) {
  const qs =
    params && Object.keys(params).length
      ? "?" +
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
          .join("&")
      : "";
  return `${BASE}${path}${qs}`;
}

async function parse(res) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok || (body && body.ok === false)) {
    const err = body?.error || {};
    throw new ApiError(err.message || res.statusText || "요청 실패", {
      code: err.code || `http_${res.status}`,
      status: res.status,
      details: err.details || null,
    });
  }
  return body?.data ?? body;
}

async function request(method, path, { params, json } = {}) {
  let res;
  try {
    res = await fetch(apiUrl(path, params), {
      method,
      headers: authHeaders(json ? { "Content-Type": "application/json" } : undefined),
      body: json ? JSON.stringify(json) : undefined,
    });
  } catch (e) {
    // 네트워크 자체 실패(백엔드 미기동 등)
    throw new ApiError("백엔드에 연결할 수 없습니다.", { code: "network_error", status: 0 });
  }
  return parse(res);
}

export const http = {
  get: (path, params) => request("GET", path, { params }),
  post: (path, json, params) => request("POST", path, { json, params }),
};

// 헤더를 실을 수 없는 미디어 요청(<img src> 등)용 URL. params + 토큰을 query 로 부착.
// (preview_url 은 인증·project 권한을 확인하는 binary image 응답 — DS-40 §7.6.6)
export function mediaUrl(path, params) {
  const merged = { ...(params || {}) };
  if (TOKEN) merged.token = TOKEN;
  return apiUrl(path, merged);
}

// multipart/form-data 업로드(WG-MSG-06). fetch 는 업로드 progress 노출이 어려워 XHR 사용.
// 성공 시 봉투 해제된 data 반환, 실패는 ApiError(code/status) throw — http.post 와 동일 규약.
export function uploadMultipart(path, formData, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl(path));
    if (TOKEN) xhr.setRequestHeader("Authorization", `Bearer ${TOKEN}`);
    // Content-Type 은 브라우저가 boundary 와 함께 자동 설정(직접 지정 금지)
    if (xhr.upload && typeof onProgress === "function") {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
    }
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300 && !(body && body.ok === false)) {
        resolve(body?.data ?? body);
      } else {
        const err = (body && body.error) || {};
        reject(
          new ApiError(err.message || xhr.statusText || "업로드 실패", {
            code: err.code || `http_${xhr.status}`,
            status: xhr.status,
            details: err.details || null,
          })
        );
      }
    };
    xhr.onerror = () => reject(new ApiError("백엔드에 연결할 수 없습니다.", { code: "network_error", status: 0 }));
    xhr.send(formData);
  });
}

// WebSocket URL(WG-MSG-05). 우선순위: VITE_WS_BASE(직접 연결) → VITE_API_BASE → 동일 출처(프록시).
// token 은 query 로 전달. http→ws, https→wss 로 보정(ws/wss 베이스는 그대로 유지).
export function wsUrl(path, params) {
  const base =
    WS_BASE ||
    BASE ||
    (typeof window !== "undefined" ? window.location.origin : "http://localhost:1420");
  const u = new URL(base + path);
  if (u.protocol === "https:") u.protocol = "wss:";
  else if (u.protocol === "http:") u.protocol = "ws:";
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
    }
  }
  if (TOKEN) u.searchParams.set("token", TOKEN);
  return u.toString();
}

export const hasToken = !!TOKEN;
