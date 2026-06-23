// 웹 채팅 이미지 첨부 클라이언트 사전검증 (DV-91, 요구 16-1).
// 부수효과·프레임워크 의존 없음 → node 단위 테스트(tests/selftest.mjs) 가능.
// 제약 근거: DS-40 §7.6.3 / DS-60 §5.4.2 — PNG·JPEG·WebP·GIF, 파일당 10MiB, 메시지당 5개.

export const IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
export const IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "gif"];
export const MAX_ATTACH_BYTES = 10 * 1024 * 1024; // 10 MiB
export const MAX_ATTACH_COUNT = 5;

function extOf(name) {
  const n = String(name || "");
  const i = n.lastIndexOf(".");
  return i < 0 ? "" : n.slice(i + 1).toLowerCase();
}

// MIME 우선 판정, 일부 환경(클립보드 blob)에서 type 비면 확장자로 보조 판정.
export function isAllowedImageType(file) {
  if (!file) return false;
  if (file.type && IMAGE_MIME_TYPES.includes(file.type)) return true;
  if (!file.type) return IMAGE_EXTS.includes(extOf(file.name));
  return false; // type 이 있는데 허용목록 밖이면 거부
}

// 파일 1개 검증 → { ok:true } 또는 { ok:false, code, message }
export function validateImageFile(file) {
  if (!file) return { ok: false, code: "invalid", message: "파일이 없습니다." };
  if (!isAllowedImageType(file)) {
    return { ok: false, code: "unsupported_type", message: "지원하지 않는 형식입니다 (PNG·JPG·WebP·GIF)." };
  }
  if (typeof file.size === "number" && file.size > MAX_ATTACH_BYTES) {
    return { ok: false, code: "too_large", message: "파일이 10MiB를 초과합니다." };
  }
  return { ok: true };
}

// 현재 currentCount 개가 있을 때 addCount 개를 더 추가할 수 있는지(메시지당 5개 한도).
export function canAddCount(currentCount, addCount = 1) {
  return currentCount + addCount <= MAX_ATTACH_COUNT;
}

// 남은 추가 가능 개수
export function remainingSlots(currentCount) {
  return Math.max(0, MAX_ATTACH_COUNT - currentCount);
}
