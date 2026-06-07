// HTTP 클라이언트 — DS-40 공통 규칙(§3) 처리.
//  - Base URL: VITE_API_BASE (기본 '' = 동일 출처, vite dev proxy 가 /api → 백엔드 전달)
//  - 인증: VITE_API_TOKEN 있으면 Authorization: Bearer 헤더 (DS-40 §3.2). 로컬 dev 는 미설정 가능.
//  - 봉투 해제: 성공 {ok:true,data} → data 반환. 실패는 ApiError throw (code/message/status).
//  - DB 미가동 등 503 은 ApiError(status=503, code) 로 전달 → 상위에서 degraded 처리.

const BASE = (import.meta.env?.VITE_API_BASE ?? "").replace(/\/$/, "");
const TOKEN = import.meta.env?.VITE_API_TOKEN || null;

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

// WebSocket URL(WG-MSG-05). 동일 출처 기준 ws(s):// 로 변환. token 은 query 로 전달.
export function wsUrl(path, params) {
  const base =
    BASE ||
    (typeof window !== "undefined" ? window.location.origin : "http://localhost:1420");
  const u = new URL(base + path);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
    }
  }
  if (TOKEN) u.searchParams.set("token", TOKEN);
  return u.toString();
}

export const hasToken = !!TOKEN;
