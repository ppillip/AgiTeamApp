//! WG-MSG-06 이미지 첨부 업로드/프리뷰. 레퍼런스: services/{attachment_service,image_meta}.py.
//! magic bytes 로 형식·치수 재검증(content sniffing 방어). 저장 = <project_root>/.agiteam/webgui/uploads/images/.
//! 시각/uuid 는 http 가 주입(core 순수 유지). 시각은 epoch 초(ISO 포맷은 bundle4).

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::repo::ApiError;

fn aerr(code: &'static str, http: u16) -> ApiError {
    ApiError::new(code, http, "attachment error")
}

/// epoch 초 → ISO-8601 UTC ("2026-06-16T07:00:00Z"). chrono 무의존 (civil-from-days).
pub fn epoch_to_iso(epoch: i64) -> String {
    let days = epoch.div_euclid(86_400);
    let secs = epoch.rem_euclid(86_400);
    let (h, mi, s) = (secs / 3600, (secs % 3600) / 60, secs % 60);
    // Howard Hinnant civil_from_days
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0,146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0,399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0,365]
    let mp = (5 * doy + 2) / 153; // [0,11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1,31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1,12]
    let year = if m <= 2 { y + 1 } else { y };
    format!("{year:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

#[derive(Debug, Clone)]
pub struct ImageInfo {
    pub fmt: String,  // png/jpg/webp/gif
    pub mime: String,
    pub width: u32,
    pub height: u32,
}

fn be_u32(b: &[u8]) -> u32 {
    u32::from_be_bytes([b[0], b[1], b[2], b[3]])
}
fn be_u16(b: &[u8]) -> u16 {
    u16::from_be_bytes([b[0], b[1]])
}
fn le_u16(b: &[u8]) -> u16 {
    u16::from_le_bytes([b[0], b[1]])
}

/// magic bytes 로 이미지 판정 + 치수 추출 (Python detect_image 정합).
pub fn detect_image(data: &[u8]) -> Option<ImageInfo> {
    // PNG
    if data.len() >= 24 && &data[..8] == b"\x89PNG\r\n\x1a\n" && &data[12..16] == b"IHDR" {
        let (w, h) = (be_u32(&data[16..20]), be_u32(&data[20..24]));
        if w > 0 && h > 0 {
            return Some(ImageInfo { fmt: "png".into(), mime: "image/png".into(), width: w, height: h });
        }
    }
    // GIF
    if data.len() >= 10 && (&data[..6] == b"GIF87a" || &data[..6] == b"GIF89a") {
        let (w, h) = (le_u16(&data[6..8]) as u32, le_u16(&data[8..10]) as u32);
        if w > 0 && h > 0 {
            return Some(ImageInfo { fmt: "gif".into(), mime: "image/gif".into(), width: w, height: h });
        }
    }
    // JPEG
    if data.len() >= 4 && data[0] == 0xFF && data[1] == 0xD8 {
        let mut i = 2usize;
        let n = data.len();
        while i + 9 < n {
            if data[i] != 0xFF {
                i += 1;
                continue;
            }
            let marker = data[i + 1];
            // SOF0..SOF15 (C0~CF) 제외 C4/C8/CC
            if (0xC0..=0xCF).contains(&marker) && marker != 0xC4 && marker != 0xC8 && marker != 0xCC {
                let h = be_u16(&data[i + 5..i + 7]) as u32;
                let w = be_u16(&data[i + 7..i + 9]) as u32;
                if w > 0 && h > 0 {
                    return Some(ImageInfo { fmt: "jpg".into(), mime: "image/jpeg".into(), width: w, height: h });
                }
                return None;
            }
            if marker == 0xD8 || marker == 0xD9 || (0xD0..=0xD7).contains(&marker) {
                i += 2;
                continue;
            }
            // segment with length
            if i + 3 >= n {
                break;
            }
            let seg_len = be_u16(&data[i + 2..i + 4]) as usize;
            i += 2 + seg_len;
        }
    }
    // WebP (RIFF....WEBP) — VP8/VP8L/VP8X 치수
    if data.len() >= 30 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        let fourcc = &data[12..16];
        if fourcc == b"VP8 " {
            let w = (le_u16(&data[26..28]) & 0x3FFF) as u32;
            let h = (le_u16(&data[28..30]) & 0x3FFF) as u32;
            if w > 0 && h > 0 {
                return Some(ImageInfo { fmt: "webp".into(), mime: "image/webp".into(), width: w, height: h });
            }
        }
        // VP8L/VP8X: 치수 파싱 생략, 형식만 인정(1x1 fallback)
        return Some(ImageInfo { fmt: "webp".into(), mime: "image/webp".into(), width: 0, height: 0 });
    }
    None
}

fn canon_ext(declared: &str) -> Option<&'static str> {
    match declared.to_lowercase().as_str() {
        "png" => Some("png"),
        "jpg" | "jpeg" => Some("jpg"),
        "webp" => Some("webp"),
        "gif" => Some("gif"),
        _ => None,
    }
}

fn ext_consistent(declared: Option<&str>, fmt: &str) -> bool {
    match declared.and_then(canon_ext) {
        None => true, // 선언 없거나 검사 불가 → magic 우선
        Some(c) => c == fmt,
    }
}

pub struct AttachmentService {
    store_dir: PathBuf,
}

impl AttachmentService {
    /// project_root → 저장 디렉터리.
    pub fn new(project_root: &Path) -> Self {
        let store_dir = project_root.join(".agiteam/webgui/uploads/images");
        Self { store_dir }
    }

    fn public_dict(&self, meta: &Value) -> Value {
        let id = meta["attachment_id"].as_str().unwrap_or("");
        json!({
            "attachment_id": meta["attachment_id"],
            "client_attachment_id": meta["client_attachment_id"],
            "kind": "image",
            "filename": meta["filename"],
            "mime_type": meta["mime_type"],
            "size_bytes": meta["size_bytes"],
            "width": meta["width"],
            "height": meta["height"],
            "sha256": meta["sha256"],
            "preview_url": format!("/api/webgui/message-attachments/{id}/preview"),
            "expires_at": meta["expires_at"].as_i64().map(epoch_to_iso),
        })
    }

    /// 업로드 저장. attachment_id/now_epoch/sha_hex 는 http 주입.
    #[allow(clippy::too_many_arguments)]
    pub fn save(
        &self,
        project_id: &str,
        data: &[u8],
        declared_filename: Option<&str>,
        client_attachment_id: Option<&str>,
        attachment_id: &str,
        now_epoch: i64,
        ttl_seconds: i64,
        max_bytes: usize,
        sha_hex: &str,
    ) -> Result<Value, ApiError> {
        if data.is_empty() {
            return Err(aerr("invalid_image", 400));
        }
        if data.len() > max_bytes {
            return Err(aerr("attachment_too_large", 413));
        }
        let declared_ext = declared_filename.and_then(|f| f.rsplit_once('.').map(|(_, e)| e.to_string()));
        if let Some(de) = &declared_ext {
            if canon_ext(de).is_none() {
                return Err(aerr("unsupported_image_type", 415));
            }
        }
        let info = detect_image(data).ok_or_else(|| aerr("invalid_image", 400))?;
        if !ext_consistent(declared_ext.as_deref(), &info.fmt) {
            return Err(aerr("unsupported_image_type", 415));
        }

        let prefix = if declared_filename.is_some() { "upload" } else { "paste" };
        let filename = format!("{prefix}-{now_epoch}-{}.{}", &sha_hex[..8.min(sha_hex.len())], info.fmt);
        let expires_at = now_epoch + ttl_seconds;

        fs::create_dir_all(&self.store_dir).map_err(|_| aerr("attachment_storage_unavailable", 500))?;
        let abs_path = self.store_dir.join(&filename);
        let meta = json!({
            "attachment_id": attachment_id,
            "client_attachment_id": client_attachment_id,
            "project_id": project_id,
            "kind": "image",
            "filename": filename,
            "mime_type": info.mime,
            "size_bytes": data.len(),
            "width": info.width,
            "height": info.height,
            "sha256": sha_hex,
            "created_at": now_epoch,
            "expires_at": expires_at,
        });
        fs::write(&abs_path, data).map_err(|_| aerr("attachment_storage_unavailable", 500))?;
        fs::write(self.store_dir.join(format!("{attachment_id}.json")), serde_json::to_vec(&meta).unwrap())
            .map_err(|_| aerr("attachment_storage_unavailable", 500))?;
        Ok(self.public_dict(&meta))
    }

    /// attachment_id → (abs_path, mime). project 소유·TTL·경로 검증.
    pub fn resolve(
        &self,
        project_id: &str,
        attachment_id: &str,
        now_epoch: i64,
    ) -> Result<(PathBuf, String), ApiError> {
        if !attachment_id.starts_with("att_") || attachment_id.len() > 64 {
            return Err(aerr("attachment_not_found", 404));
        }
        let sidecar = self.store_dir.join(format!("{attachment_id}.json"));
        if !sidecar.exists() {
            return Err(aerr("attachment_not_found", 404));
        }
        let meta: Value = match fs::read(&sidecar).ok().and_then(|b| serde_json::from_slice(&b).ok()) {
            Some(m) => m,
            None => return Err(aerr("attachment_not_found", 404)),
        };
        if meta["project_id"].as_str() != Some(project_id) {
            return Err(aerr("attachment_not_found", 404));
        }
        let expires = meta["expires_at"].as_i64().unwrap_or(0);
        if expires <= now_epoch {
            return Err(aerr("attachment_expired", 410));
        }
        let filename = meta["filename"].as_str().unwrap_or("");
        // 파일명 주입 방어: 단순 파일명만 허용
        if filename.contains('/') || filename.contains("..") {
            return Err(aerr("attachment_not_found", 404));
        }
        let abs = self.store_dir.join(filename);
        if !abs.exists() {
            return Err(aerr("attachment_expired", 410));
        }
        Ok((abs, meta["mime_type"].as_str().unwrap_or("application/octet-stream").to_string()))
    }
}
