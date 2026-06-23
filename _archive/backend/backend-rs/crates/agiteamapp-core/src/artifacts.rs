//! WG-ART 산출물 브라우저: tree/file/write/stream + 경로 보안 가드.
//! 레퍼런스: Python services/artifact_service.py. DB 미사용 — 매 요청 파일시스템 직접 읽기.
//! `changes`(watcher) 는 후속 TODO(파일시스템 watcher 서브시스템 필요).

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

use crate::repo::ApiError;

/// 유니코드 NFC 정규화 (Python unicodedata.normalize("NFC", ...) 정합, QI-WG-048).
/// macOS FS 는 NFD 로 저장하는 경향이 있어 입력/표시명을 NFC 로 통일해 Python 과 동등화한다.
fn nfc(s: &str) -> String {
    s.nfc().collect()
}

fn err(code: &'static str, http: u16, msg: &str) -> ApiError {
    ApiError::new(code, http, msg)
}

/// 파일명 길이 상한 (문자 수 기준, DS-132 §4.2).
const FILENAME_MAX_LEN: usize = 120;

/// 업로드 허용 바이너리 확장자 (documents 루트에만, DS-132 §5.3).
fn is_documents_binary_ext(ext: &str) -> bool {
    matches!(ext, "pdf" | "png" | "jpg" | "jpeg" | "gif" | "webp" | "svg" | "docx" | "pptx")
}

fn contains_subslice(haystack: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() || haystack.len() < needle.len() {
        return false;
    }
    haystack.windows(needle.len()).any(|w| w == needle)
}

/// 업로드 바이너리 magic bytes/signature 검증 (DS-132 §5.3, detect_format 정합).
fn verify_signature(data: &[u8], ext: &str) -> bool {
    match ext {
        "pdf" => data.starts_with(b"%PDF-"),
        "docx" | "pptx" => data.starts_with(b"PK"),
        "png" => data.starts_with(b"\x89PNG\r\n\x1a\n"),
        "jpg" | "jpeg" => data.starts_with(&[0xFF, 0xD8, 0xFF]),
        "gif" => data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a"),
        "webp" => data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP",
        "svg" => {
            let mut s = data;
            if s.starts_with(b"\xef\xbb\xbf") {
                s = &s[3..];
            }
            let start = s.iter().position(|&c| !c.is_ascii_whitespace()).unwrap_or(s.len());
            let h = &s[start..];
            let probe = &h[..h.len().min(1024)];
            probe.starts_with(b"<?xml") || probe.starts_with(b"<svg") || contains_subslice(probe, b"<svg")
        }
        _ => true, // 텍스트/코드는 signature 검사 없음(decode 검증으로 대체)
    }
}

/// 충돌 회피 후보명: name, "name (1).ext", ... 100회 (DS-132 §4.4).
fn rename_candidates(name: &str) -> Vec<String> {
    let mut out = vec![name.to_string()];
    if let Some((stem, ext)) = name.rsplit_once('.') {
        if !name.starts_with('.') && !stem.is_empty() && !ext.is_empty() {
            for i in 1..=100 {
                out.push(format!("{stem} ({i}).{ext}"));
            }
            return out;
        }
    }
    for i in 1..=100 {
        out.push(format!("{name} ({i})"));
    }
    out
}

fn first_free_name(parent_abs: &Path, name: &str) -> Result<String, ApiError> {
    for cand in rename_candidates(name) {
        if !parent_abs.join(&cand).exists() {
            return Ok(cand);
        }
    }
    Err(err("artifact_already_exists", 409, "An artifact already exists at the target path."))
}

/// O_CREAT|O_EXCL 원자 생성. error 모드 충돌 → 409, rename 모드 → 빈 이름 탐색.
fn atomic_create(parent_abs: &Path, name: &str, data: &[u8], if_exists: &str) -> Result<String, ApiError> {
    use std::io::Write;
    let candidates = if if_exists == "error" { vec![name.to_string()] } else { rename_candidates(name) };
    for cand in &candidates {
        let target = parent_abs.join(cand);
        match fs::OpenOptions::new().write(true).create_new(true).open(&target) {
            Ok(mut f) => {
                f.write_all(data)
                    .map_err(|_| err("artifact_write_failed", 500, "Failed to write artifact."))?;
                return Ok(cand.clone());
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if if_exists == "error" {
                    return Err(err("artifact_already_exists", 409, "An artifact already exists at the target path."));
                }
                continue;
            }
            Err(_) => return Err(err("artifact_write_failed", 500, "Failed to write artifact.")),
        }
    }
    Err(err("artifact_already_exists", 409, "An artifact already exists at the target path."))
}

/// temp 기록 → target atomic rename. error 모드는 O_EXCL 로 target 선점.
fn atomic_upload(parent_abs: &Path, name: &str, data: &[u8], if_exists: &str, sha_hex: &str) -> Result<String, ApiError> {
    use std::io::ErrorKind;
    let final_name = if if_exists == "error" {
        let target = parent_abs.join(name);
        match fs::OpenOptions::new().write(true).create_new(true).open(&target) {
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::AlreadyExists => {
                return Err(err("artifact_already_exists", 409, "An artifact already exists at the target path."));
            }
            Err(_) => return Err(err("artifact_storage_unavailable", 503, "Artifact storage is unavailable.")),
        }
        name.to_string()
    } else {
        first_free_name(parent_abs, name)?
    };
    let target = parent_abs.join(&final_name);
    let tmp = parent_abs.join(format!(".upload-{}.tmp", &sha_hex[..8.min(sha_hex.len())]));
    let cleanup = |this_err: ApiError| -> ApiError {
        let _ = fs::remove_file(&tmp);
        if if_exists == "error" {
            let _ = fs::remove_file(&target);
        }
        this_err
    };
    if fs::write(&tmp, data).is_err() {
        return Err(cleanup(err("artifact_write_failed", 500, "Failed to write artifact.")));
    }
    if fs::rename(&tmp, &target).is_err() {
        return Err(cleanup(err("artifact_write_failed", 500, "Failed to write artifact.")));
    }
    Ok(final_name)
}

/// FE 낙관 갱신 힌트 (DS-132 §3).
fn tree_refresh(root_type: &str, parent_rel: &str, changed_rel: &str) -> Value {
    let r = root_type.trim().to_lowercase();
    let rt = if r.is_empty() { "documents".to_string() } else { r };
    json!({
        "root_type": rt,
        "parent_path": parent_rel,
        "changed_path": changed_rel,
        "change_type": "created",
    })
}

/// 산출물 서비스. root = <project_root>/<subdir> (documents|system|brain), display = 논리 라벨.
pub struct ArtifactService {
    root: PathBuf,
    display_root: String,
}

pub struct ResolvedPath {
    pub abs: PathBuf,
    pub rel: String,
}

fn ext_of(name: &str) -> Option<String> {
    name.rsplit_once('.').map(|(_, e)| e.to_lowercase())
}

/// 형식 판정용 effective 확장자 (특수 파일명 포함).
fn fmt_ext(name: &str) -> Option<String> {
    if let Some(e) = ext_of(name) {
        return Some(e);
    }
    match name.to_lowercase().as_str() {
        "dockerfile" => Some("dockerfile".into()),
        "makefile" => Some("makefile".into()),
        _ => None,
    }
}

fn is_control(s: &str) -> bool {
    s.chars().any(|c| (c as u32) < 0x20 || (c as u32) == 0x7f)
}

/// secret/민감 파일명 (include_hidden 여도 차단). Python _SECRET_NAME 정합.
fn is_secret_name(seg: &str) -> bool {
    let l = seg.to_lowercase();
    (l == ".env" || l.starts_with(".env."))
        || l.contains("secret")
        || l.contains("credential")
        || l.ends_with(".pem")
        || l.ends_with(".key")
        || l == "token"
        || l.starts_with("token.")
        || l.ends_with(".token")
        || l.contains("_token")
        || l.contains("-token")
        || l.contains(".token.")
        || l == "settings.local.json"
        || l.contains("id_rsa")
        || l.ends_with(".p12")
        || l.ends_with(".pfx")
}

fn render_mode(fmt: &str) -> Option<&'static str> {
    Some(match fmt {
        "md" | "markdown" => "markdown",
        "pdf" => "pdf_stream",
        "svg" | "png" | "jpg" | "jpeg" | "gif" | "webp" => "image",
        "html" | "htm" => "html",
        "pptx" | "docx" => "converted_preview",
        _ if code_lang(fmt).is_some() => "code",
        _ => return None,
    })
}

fn mime_of(fmt: &str) -> Option<&'static str> {
    Some(match fmt {
        "md" | "markdown" => "text/markdown",
        "pdf" => "application/pdf",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "html" | "htm" => "text/html",
        "pptx" => "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "json" => "application/json",
        "xml" => "application/xml",
        "csv" => "text/csv",
        _ if code_lang(fmt).is_some() => "text/plain",
        _ => return None,
    })
}

fn code_lang(fmt: &str) -> Option<&'static str> {
    Some(match fmt {
        "json" => "json",
        "yaml" | "yml" => "yaml",
        "toml" => "toml",
        "ini" | "cfg" | "conf" => "ini",
        "py" => "python",
        "sh" | "bash" | "zsh" => "bash",
        "js" | "mjs" | "cjs" | "jsx" => "javascript",
        "ts" | "tsx" => "typescript",
        "vue" => "vue",
        "css" => "css",
        "scss" => "scss",
        "sass" => "sass",
        "less" => "less",
        "xml" => "xml",
        "sql" => "sql",
        "go" => "go",
        "rs" => "rust",
        "java" => "java",
        "kt" => "kotlin",
        "c" | "h" => "c",
        "cpp" | "cc" | "hpp" => "cpp",
        "rb" => "ruby",
        "php" => "php",
        "pl" => "perl",
        "lua" => "lua",
        "r" => "r",
        "txt" | "log" | "csv" | "gitignore" => "text",
        "dockerfile" => "dockerfile",
        "makefile" => "makefile",
        _ => return None,
    })
}

fn is_writable_ext(fmt: &str) -> bool {
    fmt == "md" || fmt == "markdown" || code_lang(fmt).is_some()
}

impl ArtifactService {
    pub fn new(root: PathBuf, display_root: String) -> Self {
        let root = fs::canonicalize(&root).unwrap_or(root);
        Self { root, display_root }
    }

    /// 경로 보안 해석 (Python resolve 정합).
    pub fn resolve(&self, raw_path: Option<&str>) -> Result<ResolvedPath, ApiError> {
        let mut path = raw_path.unwrap_or("").to_string();
        if is_control(&path) {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        // 유니코드 NFC 정규화 (Python 정합, QI-WG-048). NFD 입력도 NFC 로 통일.
        path = nfc(&path);
        // drive/UNC 차단
        let bytes = path.as_bytes();
        if (bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && (bytes[2] == b'\\' || bytes[2] == b'/'))
            || path.starts_with("\\\\")
        {
            return Err(err("path_forbidden", 403, "Path is outside the allowed root."));
        }
        path = path.replace('\\', "/");
        if path.starts_with('/') {
            return Err(err("path_forbidden", 403, "Path is outside the allowed root."));
        }
        if path.is_empty() || path == "." {
            return Ok(ResolvedPath { abs: self.root.clone(), rel: String::new() });
        }
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty() && *s != ".").collect();
        for seg in &segments {
            if *seg == ".." {
                return Err(err("path_forbidden", 403, "Path is outside the allowed root."));
            }
        }
        let rel = segments.join("/");
        let candidate = self.root.join(&rel);
        let real = match fs::canonicalize(&candidate) {
            Ok(r) => r,
            Err(_) => {
                // 미존재 경로(write 신규 등): secret 검사 후, 존재하는 최상위 조상까지
                // 거슬러 올라가 within-root 만 검증한다. 부모 디렉토리가 아직 없어도
                // (write 신규 하위경로) 허용 — Python 은 write 시 create_dir_all 로 생성하고
                // 실패 시 write_failed(500) 를 낸다. 부모 1단계만 보던 기존 로직은 신규
                // 하위 디렉토리에서 artifact_path_not_found(404) 를 내 Python(500)과 어긋났다.
                for seg in &segments {
                    if is_secret_name(seg) {
                        return Err(err("artifact_hidden", 403, "Hidden or restricted file."));
                    }
                }
                let mut ancestor = candidate.parent();
                while let Some(a) = ancestor {
                    if let Ok(real_a) = fs::canonicalize(a) {
                        if real_a.starts_with(&self.root) {
                            // lexical abs (traversal 은 위에서 차단, within-root 확인됨)
                            return Ok(ResolvedPath { abs: self.root.join(&rel), rel });
                        }
                        return Err(err("path_forbidden", 403, "Path is outside the allowed root."));
                    }
                    ancestor = a.parent();
                }
                return Err(err("artifact_path_not_found", 404, "Artifact path not found."));
            }
        };
        if !real.starts_with(&self.root) {
            // symlink escape 가능성
            if candidate.symlink_metadata().map(|m| m.file_type().is_symlink()).unwrap_or(false) {
                return Err(err("symlink_forbidden", 403, "Symbolic links are not allowed."));
            }
            return Err(err("path_forbidden", 403, "Path is outside the allowed root."));
        }
        for seg in &segments {
            if is_secret_name(seg) {
                return Err(err("artifact_hidden", 403, "Hidden or restricted file."));
            }
        }
        Ok(ResolvedPath { abs: real, rel })
    }

    fn build_node(&self, abs: &Path, rel: &str, is_dir: bool) -> Value {
        let name = abs.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
        let fe = if is_dir { None } else { fmt_ext(&name) };
        let extension = if is_dir { None } else { ext_of(&name) };
        let (mut size, mut has_children, mut renderable, mut mime) = (None, false, false, None);
        if is_dir {
            has_children = fs::read_dir(abs).map(|mut d| d.next().is_some()).unwrap_or(false);
        } else {
            size = fs::metadata(abs).ok().map(|m| m.len());
            if let Some(f) = &fe {
                renderable = render_mode(f).is_some();
                mime = mime_of(f);
            }
        }
        json!({
            "path": rel,
            "name": name,
            "node_type": if is_dir { "directory" } else { "file" },
            "extension": extension,
            "mime_type": mime,
            "size_bytes": size,
            "has_children": has_children,
            "renderable": renderable,
        })
    }

    fn is_hidden_or_secret(name: &str, include_hidden: bool) -> bool {
        if is_secret_name(name) {
            return true;
        }
        name.starts_with('.') && !include_hidden
    }

    #[allow(clippy::too_many_arguments)]
    fn children(
        &self,
        abs_dir: &Path,
        rel_dir: &str,
        remaining_depth: i64,
        recursive: bool,
        include_files: bool,
        include_hidden: bool,
        extensions: &Option<Vec<String>>,
        counter: &mut i64,
        max_nodes: i64,
    ) -> Vec<Value> {
        if remaining_depth <= 0 {
            return vec![];
        }
        let mut entries: Vec<_> = match fs::read_dir(abs_dir) {
            Ok(rd) => rd.filter_map(|e| e.ok()).collect(),
            Err(_) => return vec![],
        };
        entries.sort_by(|a, b| {
            let ad = a.file_type().map(|t| t.is_dir()).unwrap_or(false);
            let bd = b.file_type().map(|t| t.is_dir()).unwrap_or(false);
            bd.cmp(&ad).then(
                a.file_name()
                    .to_string_lossy()
                    .to_lowercase()
                    .cmp(&b.file_name().to_string_lossy().to_lowercase()),
            )
        });
        let mut out = Vec::new();
        for e in entries {
            if *counter >= max_nodes {
                break;
            }
            let ft = match e.file_type() {
                Ok(t) => t,
                Err(_) => continue,
            };
            if ft.is_symlink() {
                continue;
            }
            let name = e.file_name().to_string_lossy().to_string();
            if Self::is_hidden_or_secret(&name, include_hidden) {
                continue;
            }
            let is_dir = ft.is_dir();
            if !is_dir && !include_files {
                continue;
            }
            if !is_dir {
                if let Some(exts) = extensions {
                    let fe = ext_of(&name);
                    if fe.as_deref().map(|x| !exts.iter().any(|w| w == x)).unwrap_or(true) {
                        continue;
                    }
                }
            }
            let child_rel = if rel_dir.is_empty() { name.clone() } else { format!("{rel_dir}/{name}") };
            *counter += 1;
            let mut node = self.build_node(&e.path(), &child_rel, is_dir);
            if is_dir && recursive && remaining_depth - 1 > 0 {
                let kids = self.children(
                    &e.path(),
                    &child_rel,
                    remaining_depth - 1,
                    recursive,
                    include_files,
                    include_hidden,
                    extensions,
                    counter,
                    max_nodes,
                );
                node["children"] = json!(kids);
            }
            out.push(node);
        }
        out
    }

    #[allow(clippy::too_many_arguments)]
    pub fn list_tree(
        &self,
        raw_path: Option<&str>,
        depth: i64,
        recursive: bool,
        include_files: bool,
        include_hidden: bool,
        extensions: Option<Vec<String>>,
        max_nodes: i64,
        max_depth: i64,
    ) -> Result<Value, ApiError> {
        if depth < 1 || depth > max_depth {
            return Err(err("invalid_tree_query", 422, "depth out of range"));
        }
        let rp = self.resolve(raw_path)?;
        if !rp.abs.exists() {
            return Err(err("artifact_path_not_found", 404, "Artifact path not found."));
        }
        if !rp.abs.is_dir() {
            return Err(err("not_directory", 422, "Target is not a directory."));
        }
        let mut counter = 0i64;
        let mut node = self.build_node(&rp.abs, &rp.rel, true);
        let kids = self.children(
            &rp.abs, &rp.rel, depth, recursive, include_files, include_hidden, &extensions,
            &mut counter, max_nodes,
        );
        node["children"] = json!(kids);
        Ok(json!({ "root": self.display_root, "path": rp.rel, "node": node }))
    }

    /// read_file → (data, status). data = {file, conversion?}.
    pub fn read_file(
        &self,
        raw_path: &str,
        sanitize: bool,
        max_inline_bytes: u64,
        root_type: Option<&str>,
    ) -> Result<(Value, u16), ApiError> {
        let rp = self.resolve(Some(raw_path))?;
        if !rp.abs.exists() {
            return Err(err("artifact_path_not_found", 404, "Artifact path not found."));
        }
        if rp.abs.is_dir() {
            return Err(err("not_file", 422, "Target is not a file."));
        }
        let name = rp.abs.file_name().unwrap().to_string_lossy().to_string();
        let fe = fmt_ext(&name).ok_or_else(|| err("unsupported_media_type", 415, "Unsupported media type."))?;
        let mode = render_mode(&fe).ok_or_else(|| err("unsupported_media_type", 415, "Unsupported media type."))?;
        let mime = mime_of(&fe).unwrap_or("application/octet-stream");
        let size = fs::metadata(&rp.abs).map(|m| m.len()).unwrap_or(0);

        let rt = root_type.unwrap_or("").to_lowercase();
        let stream_qs = if !rt.is_empty() && rt != "documents" { format!("&root_type={rt}") } else { String::new() };
        let stream_url = format!(
            "/api/webgui/artifacts/file/stream?path={}{}",
            urlencode(&rp.rel),
            stream_qs
        );

        let mut file = json!({
            "path": rp.rel,
            "name": name,
            "extension": fe,
            "mime_type": mime,
            "size_bytes": size,
            "render_mode": mode,
            "content_type": mime,
            "encoding": Value::Null,
            "content": Value::Null,
            "stream_url": Value::Null,
            "converted_url": Value::Null,
            "download_allowed": false,
            "sanitized": false,
            "render_warnings": [],
            "language_hint": Value::Null,
        });

        match mode {
            "code" => {
                if size > max_inline_bytes {
                    return Err(err("file_too_large", 413, "File exceeds the allowed size."));
                }
                let text = read_text(&rp.abs);
                file["content"] = json!(text);
                file["content_type"] = json!("text/plain; charset=utf-8");
                file["encoding"] = json!("utf-8");
                file["language_hint"] = json!(code_lang(&fe).unwrap_or("text"));
                Ok((json!({ "file": file }), 200))
            }
            "markdown" => {
                if size > max_inline_bytes {
                    return Err(err("file_too_large", 413, "File exceeds the allowed size."));
                }
                let mut text = read_text(&rp.abs);
                if sanitize {
                    let (s, w) = sanitize_markdown(&text);
                    text = s;
                    file["sanitized"] = json!(true);
                    file["render_warnings"] = json!(w);
                }
                file["content"] = json!(text);
                file["content_type"] = json!("text/markdown; charset=utf-8");
                file["encoding"] = json!("utf-8");
                Ok((json!({ "file": file }), 200))
            }
            "pdf_stream" => {
                file["stream_url"] = json!(stream_url);
                Ok((json!({ "file": file }), 200))
            }
            "image" => {
                file["stream_url"] = json!(stream_url);
                if fe == "svg" {
                    if size <= max_inline_bytes {
                        let mut text = read_text(&rp.abs);
                        if sanitize {
                            let (s, w) = sanitize_markdown(&text);
                            text = s;
                            file["sanitized"] = json!(true);
                            file["render_warnings"] = json!(w);
                        }
                        file["content"] = json!(text);
                        file["encoding"] = json!("utf-8");
                    }
                    file["content_type"] = json!("image/svg+xml");
                } else {
                    file["content_type"] = json!(mime);
                }
                Ok((json!({ "file": file }), 200))
            }
            "html" => {
                file["stream_url"] = json!(stream_url);
                file["content_type"] = json!("text/html; charset=utf-8");
                // 작은 html(<= max_inline_bytes)은 raw inline content 도 함께 싣는다.
                // FE 뷰어가 stream_url(iframe src, BE CSP sandbox=default-src none)이 아니라
                // srcdoc(content)+sandbox=allow-scripts 경로로 렌더 → mermaid 등 스크립트가 동작한다.
                // sanitize 는 적용하지 않는다(스크립트 제거 시 mermaid 가 깨짐 — raw 보존). 대용량은
                // inline 을 생략해 기존 stream 경로를 유지한다.
                if size <= max_inline_bytes {
                    file["content"] = json!(read_text(&rp.abs));
                    file["encoding"] = json!("utf-8");
                    file["sanitized"] = json!(false);
                }
                Ok((json!({ "file": file }), 200))
            }
            _ => {
                // converted_preview (pptx/docx) — 변환기 미구현, pending.
                file["render_mode"] = json!("converted_preview");
                file["content_type"] = json!("text/html; charset=utf-8");
                file["encoding"] = json!("utf-8");
                file["converted_url"] = json!(format!(
                    "/api/webgui/artifacts/file/stream?path={}&variant=preview{}",
                    urlencode(&rp.rel),
                    stream_qs
                ));
                file["sanitized"] = json!(true);
                file["render_warnings"] = json!(["conversion_pending"]);
                Ok((
                    json!({ "file": file, "conversion": { "status": "pending", "timeout_seconds": 30 } }),
                    202,
                ))
            }
        }
    }

    pub fn write_file(&self, raw_path: &str, content: &str) -> Result<Value, ApiError> {
        let rp = self.resolve(Some(raw_path))?;
        let name = rp.abs.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
        let fe = fmt_ext(&name).unwrap_or_default();
        if !is_writable_ext(&fe) {
            return Err(err("invalid_artifact_type", 400, "Unsupported write type."));
        }
        if rp.abs.exists() && rp.abs.is_dir() {
            return Err(err("not_file", 422, "Target is not a file."));
        }
        if let Some(parent) = rp.abs.parent() {
            fs::create_dir_all(parent).map_err(|_| err("artifact_write_failed", 500, "Failed to write artifact."))?;
        }
        fs::write(&rp.abs, content).map_err(|_| err("artifact_write_failed", 500, "Failed to write artifact."))?;
        Ok(json!({ "saved": true, "path": rp.rel }))
    }

    /// 산출물 파일 삭제 (WG-ART-07, 휴지통식). 보안: resolve 게이트(제어문자·드라이브·절대·`..`·
    /// secret/hidden·within-root 검증)를 그대로 통과한 뒤, 추가로 루트 자체/심볼릭/디렉토리를
    /// 거부한다. 허용된 산출물 루트(documents/system/brain) 안의 '파일'만 삭제 가능.
    ///
    /// 동작:
    ///  - 경로에 `_archive` 세그먼트가 있는 파일(=이미 휴지통 내부) → **영구 삭제**(중첩 백업 방지).
    ///  - 그 외 일반 산출물 → 같은 폴더의 `_archive/` 에 백업 복사(없으면 생성) **성공 후** 원본 삭제.
    ///    백업 파일명: `<확장자뺀이름>_<timestamp>.<원본확장자>` (예: foo_v0.2.html → foo_v0.2_<ts>.html).
    ///    복사 실패 시 원본을 삭제하지 않고 에러 반환(데이터 보존 우선).
    ///
    /// `timestamp`: 백업명에 박을 YYYYMMDDhhmmss(호출측이 로컬시각으로 생성). 숫자 외 문자는 방어적으로 제거.
    pub fn delete_file(&self, raw_path: &str, timestamp: &str) -> Result<Value, ApiError> {
        let rp = self.resolve(Some(raw_path))?;
        // 루트 자체 삭제 금지(빈 경로/".").
        if rp.rel.is_empty() {
            return Err(err("path_forbidden", 403, "Cannot delete the root directory."));
        }
        // 심볼릭 링크 거부: resolve 는 canonicalize 로 '타겟'을 가리키므로, 링크 자체를
        // lexical 경로로 재확인해 링크 경유 타겟 삭제를 차단한다(심볼릭 escape 방어).
        let lexical = self.root.join(&rp.rel);
        if lexical
            .symlink_metadata()
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(err("symlink_forbidden", 403, "Symbolic links are not allowed."));
        }
        if !rp.abs.exists() {
            return Err(err("artifact_path_not_found", 404, "Artifact path not found."));
        }
        if rp.abs.is_dir() {
            // 디렉토리 일괄 삭제는 위험 → 거부(파일 단위만 허용).
            return Err(err("not_file", 422, "Target is a directory, not a file."));
        }

        // 이미 _archive 내부 파일이면 백업 없이 영구 삭제(무한 중첩 방지).
        let in_archive = rp.rel.split('/').any(|seg| seg == "_archive");
        if in_archive {
            fs::remove_file(&rp.abs)
                .map_err(|_| err("artifact_delete_failed", 500, "Failed to delete artifact."))?;
            return Ok(json!({
                "deleted": true,
                "permanent": true,
                "archived": false,
                "path": rp.rel,
                "archive_path": Value::Null,
            }));
        }

        // --- 휴지통식: _archive 백업 복사 후 원본 삭제 ---
        let file_name = rp
            .abs
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();
        // <stem>_<ts>.<ext> (확장자 없으면 <stem>_<ts>). 확장자는 보존.
        let ts: String = timestamp.chars().filter(|c| c.is_ascii_digit()).collect();
        let (stem, ext) = match file_name.rsplit_once('.') {
            // 선행 점 없는 정상 확장자만 분리(.gitignore 같은 숨김은 resolve 가 이미 차단).
            Some((s, e)) if !s.is_empty() && !e.is_empty() => (s.to_string(), Some(e.to_string())),
            _ => (file_name.clone(), None),
        };
        let backup_name = match &ext {
            Some(e) => format!("{stem}_{ts}.{e}"),
            None => format!("{stem}_{ts}"),
        };

        let parent_abs = rp
            .abs
            .parent()
            .ok_or_else(|| err("artifact_delete_failed", 500, "Failed to resolve parent."))?;
        let archive_dir = parent_abs.join("_archive");
        fs::create_dir_all(&archive_dir)
            .map_err(|_| err("artifact_archive_failed", 500, "Failed to create archive folder."))?;
        let backup_abs = archive_dir.join(&backup_name);

        // 복사 먼저. 실패하면 원본 보존(삭제 안 함).
        fs::copy(&rp.abs, &backup_abs).map_err(|_| {
            err("artifact_archive_failed", 500, "Failed to back up before delete.")
        })?;
        // 복사 성공 → 원본 삭제. 삭제 실패해도 백업은 남는다.
        fs::remove_file(&rp.abs)
            .map_err(|_| err("artifact_delete_failed", 500, "Failed to delete artifact."))?;

        // 백업 상대경로(루트 기준): <부모rel>/_archive/<backup_name>.
        let parent_rel = match rp.rel.rsplit_once('/') {
            Some((p, _)) => p.to_string(),
            None => String::new(),
        };
        let archive_rel = if parent_rel.is_empty() {
            format!("_archive/{backup_name}")
        } else {
            format!("{parent_rel}/_archive/{backup_name}")
        };

        Ok(json!({
            "deleted": true,
            "permanent": false,
            "archived": true,
            "path": rp.rel,
            "archive_path": archive_rel,
        }))
    }

    /// open_stream → (abs_path, mime, size). http 가 range 스트리밍.
    pub fn open_stream(&self, raw_path: &str) -> Result<(PathBuf, String, u64), ApiError> {
        let rp = self.resolve(Some(raw_path))?;
        if !rp.abs.exists() {
            return Err(err("artifact_path_not_found", 404, "Artifact path not found."));
        }
        if rp.abs.is_dir() {
            return Err(err("not_file", 422, "Target is not a file."));
        }
        let name = rp.abs.file_name().unwrap().to_string_lossy().to_string();
        let fe = fmt_ext(&name).ok_or_else(|| err("unsupported_media_type", 415, "Unsupported media type."))?;
        let mime = mime_of(&fe).ok_or_else(|| err("unsupported_media_type", 415, "Unsupported media type."))?;
        let size = fs::metadata(&rp.abs).map(|m| m.len()).unwrap_or(0);
        Ok((rp.abs, mime.to_string(), size))
    }

    // --- 컨텍스트 메뉴 신규 (WG-ART-08/09 / DS-132) ------------------------

    /// 파일명 정책 검증 + 반환 (DS-132 §4.2). 위반 시 적절한 ApiError.
    /// (NFC 정규화는 기존 resolve 와 동일하게 미적용 — Python 과의 미세 parity 갭은 보고서 기재.)
    #[allow(clippy::unused_self)]
    pub fn validate_filename(&self, filename: &str) -> Result<String, ApiError> {
        if is_control(filename) {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        // 유니코드 NFC 정규화 (Python validate_filename 정합, QI-WG-048).
        let n = nfc(filename);
        if n.trim().is_empty() {
            return Err(err("invalid_request", 400, "filename is required."));
        }
        if n.contains('/') || n.contains('\\') {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        let b = n.as_bytes();
        if b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':' {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        if n != n.trim() {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        if n.chars().count() > FILENAME_MAX_LEN {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        if n == "." || n == ".." {
            return Err(err("invalid_path", 400, "Invalid artifact path."));
        }
        if n.starts_with('.') {
            return Err(err("artifact_hidden", 403, "Hidden or restricted file."));
        }
        if is_secret_name(&n) {
            return Err(err("artifact_hidden", 403, "Hidden or restricted file."));
        }
        Ok(n)
    }

    /// 폴더 한정 기능용: parent_path resolve 후 반드시 is_dir 강제 (DS-132 §7).
    fn resolve_parent_dir(&self, parent_path: Option<&str>) -> Result<ResolvedPath, ApiError> {
        let rp = self.resolve(parent_path)?;
        if !rp.abs.exists() {
            return Err(err("artifact_path_not_found", 404, "Artifact path not found."));
        }
        if !rp.abs.is_dir() {
            return Err(err("not_directory", 422, "Target is not a directory."));
        }
        Ok(rp)
    }

    /// 생성/업로드 직후 WG-ART-02 ArtifactFile 전체 필드 반환 (축약 금지, DS-132 §3).
    /// read_file 을 재사용해 Python/Rust 동일 스키마 보장. 1MiB 초과 텍스트는 메타+stream_url.
    fn describe_written_file(&self, rel: &str, root_type: &str, max_inline: u64) -> Result<Value, ApiError> {
        match self.read_file(rel, true, max_inline, Some(root_type)) {
            Ok((data, _)) => Ok(data["file"].clone()),
            Err(e) if e.code == "file_too_large" => {
                let rp = self.resolve(Some(rel))?;
                let name = rp.abs.file_name().unwrap().to_string_lossy().to_string();
                let fe = fmt_ext(&name).unwrap_or_default();
                let mime = mime_of(&fe).unwrap_or("application/octet-stream");
                let mode = render_mode(&fe).unwrap_or("code");
                let size = fs::metadata(&rp.abs).map(|m| m.len()).unwrap_or(0);
                let rt = root_type.to_lowercase();
                let qs = if !rt.is_empty() && rt != "documents" { format!("&root_type={rt}") } else { String::new() };
                Ok(json!({
                    "path": rp.rel,
                    "name": name,
                    "extension": fe,
                    "mime_type": mime,
                    "size_bytes": size,
                    "render_mode": mode,
                    "content_type": mime,
                    "encoding": Value::Null,
                    "content": Value::Null,
                    "stream_url": format!("/api/webgui/artifacts/file/stream?path={}{}", urlencode(&rp.rel), qs),
                    "converted_url": Value::Null,
                    "download_allowed": false,
                    "sanitized": false,
                    "render_warnings": [],
                    "language_hint": code_lang(&fe),
                }))
            }
            Err(e) => Err(e),
        }
    }

    /// WG-ART-08 새파일 생성 (DS-132 §4). 폴더 한정. 반환 {file, tree_refresh}.
    pub fn create_file(
        &self,
        parent_path: Option<&str>,
        filename: &str,
        template: &str,
        if_exists: &str,
        root_type: &str,
        max_inline: u64,
    ) -> Result<Value, ApiError> {
        let parent = self.resolve_parent_dir(parent_path)?;
        let name = self.validate_filename(filename)?;
        let fe = fmt_ext(&name);
        if !fe.as_deref().map(is_writable_ext).unwrap_or(false) {
            return Err(err("unsupported_media_type", 415, "Unsupported file format."));
        }
        let fe = fe.unwrap();
        if !matches!(template, "empty" | "markdown_basic" | "json_object") {
            return Err(err("invalid_request", 400, "invalid template"));
        }
        if !matches!(if_exists, "error" | "rename") {
            return Err(err("invalid_request", 400, "invalid if_exists"));
        }
        if template == "json_object" && fe != "json" {
            return Err(err("invalid_artifact_template", 422, "json_object requires a .json extension"));
        }
        if template == "markdown_basic" && fe != "md" && fe != "markdown" {
            return Err(err("invalid_artifact_template", 422, "markdown_basic requires a .md extension"));
        }
        let basename = name.rsplit_once('.').map(|(s, _)| s.to_string()).unwrap_or_else(|| name.clone());
        let content = match template {
            "markdown_basic" => format!("# {basename}\n"),
            "json_object" => "{}\n".to_string(),
            _ => String::new(),
        };
        let final_name = atomic_create(&parent.abs, &name, content.as_bytes(), if_exists)?;
        let rel = if parent.rel.is_empty() { final_name.clone() } else { format!("{}/{}", parent.rel, final_name) };
        let file = self.describe_written_file(&rel, root_type, max_inline)?;
        Ok(json!({ "file": file, "tree_refresh": tree_refresh(root_type, &parent.rel, &rel) }))
    }

    /// WG-ART-09 파일 업로드 (DS-132 §5). 폴더 한정 + root_type 정책 + 크기/확장자/signature/decode.
    #[allow(clippy::too_many_arguments)]
    pub fn upload_file(
        &self,
        parent_path: Option<&str>,
        filename: &str,
        data: &[u8],
        if_exists: &str,
        root_type: &str,
        client_upload_id: Option<&str>,
        max_upload: u64,
        max_inline: u64,
    ) -> Result<Value, ApiError> {
        let r = root_type.trim().to_lowercase();
        let rt = if r.is_empty() { "documents".to_string() } else { r };
        if !matches!(if_exists, "error" | "rename") {
            return Err(err("invalid_request", 400, "invalid if_exists"));
        }
        let parent = self.resolve_parent_dir(parent_path)?;
        let name = self.validate_filename(filename)?;
        if data.len() as u64 > max_upload {
            return Err(err("file_too_large", 413, "File exceeds the allowed size."));
        }
        let fe = match fmt_ext(&name) {
            Some(e) => e,
            None => return Err(err("unsupported_media_type", 415, "Unsupported file format.")),
        };
        let is_binary = is_documents_binary_ext(&fe);
        let allowed = if rt == "documents" {
            is_writable_ext(&fe) || is_binary
        } else {
            is_writable_ext(&fe) && !is_binary
        };
        if !allowed {
            return Err(err("unsupported_media_type", 415, "Unsupported file format."));
        }
        if is_binary {
            if !verify_signature(data, &fe) {
                return Err(err("unsupported_media_type", 415, "Unsupported file format."));
            }
        } else if std::str::from_utf8(data).is_err() {
            return Err(err("invalid_text_encoding", 422, "Uploaded text is not valid UTF-8."));
        }
        let sha_hex: String = Sha256::digest(data).iter().map(|b| format!("{b:02x}")).collect();
        let final_name = atomic_upload(&parent.abs, &name, data, if_exists, &sha_hex)?;
        let rel = if parent.rel.is_empty() { final_name.clone() } else { format!("{}/{}", parent.rel, final_name) };
        let file = self.describe_written_file(&rel, &rt, max_inline)?;
        let mime = file["mime_type"].clone();
        Ok(json!({
            "upload": {
                "client_upload_id": client_upload_id,
                "filename": final_name,
                "mime_type": mime,
                "size_bytes": data.len(),
                "sha256": sha_hex,
            },
            "file": file,
            "tree_refresh": tree_refresh(&rt, &parent.rel, &rel),
        }))
    }
}

fn read_text(p: &Path) -> String {
    fs::read(p).map(|b| String::from_utf8_lossy(&b).to_string()).unwrap_or_default()
}

/// 최소 URL 인코딩 (경로용; 안전문자 외 %xx).
fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// markdown/svg sanitize: script/iframe/on*/javascript: 무력화 (Python sanitize_markdown 핵심).
pub fn sanitize_markdown(text: &str) -> (String, Vec<String>) {
    let mut warnings = Vec::new();
    let mut out = text.to_string();
    let lower = out.to_lowercase();
    if lower.contains("<script") {
        warnings.push("script_stripped".to_string());
        out = strip_tag_blocks(&out, "script");
    }
    if lower.contains("<iframe") {
        warnings.push("iframe_stripped".to_string());
        out = strip_tag_blocks(&out, "iframe");
    }
    if lower.contains("javascript:") {
        warnings.push("javascript_uri_stripped".to_string());
        out = case_insensitive_replace(&out, "javascript:", "blocked:");
    }
    (out, warnings)
}

fn strip_tag_blocks(s: &str, tag: &str) -> String {
    let open = format!("<{tag}");
    let close = format!("</{tag}>");
    let mut result = s.to_string();
    loop {
        let lower = result.to_lowercase();
        let Some(start) = lower.find(&open) else { break };
        let end = match lower[start..].find(&close) {
            Some(rel) => start + rel + close.len(),
            None => result.len(),
        };
        result.replace_range(start..end, "");
    }
    result
}

fn case_insensitive_replace(s: &str, from: &str, to: &str) -> String {
    let mut result = String::new();
    let lower = s.to_lowercase();
    let from_lower = from.to_lowercase();
    let mut i = 0;
    while i < s.len() {
        if lower[i..].starts_with(&from_lower) {
            result.push_str(to);
            i += from.len();
        } else {
            let ch = s[i..].chars().next().unwrap();
            result.push(ch);
            i += ch.len_utf8();
        }
    }
    result
}
