//! WG-ART 산출물 브라우저: tree/file/write/stream + 경로 보안 가드.
//! 레퍼런스: Python services/artifact_service.py. DB 미사용 — 매 요청 파일시스템 직접 읽기.
//! `changes`(watcher) 는 후속 TODO(파일시스템 watcher 서브시스템 필요).

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::repo::ApiError;

fn err(code: &'static str, http: u16, msg: &str) -> ApiError {
    ApiError::new(code, http, msg)
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
                // 미존재 경로(write 신규 등): 부모까지 canonicalize 해 within-root 만 검증.
                let parent = candidate.parent().unwrap_or(&self.root);
                match fs::canonicalize(parent) {
                    Ok(p) if p.starts_with(&self.root) => {
                        // secret 검사
                        for seg in &segments {
                            if is_secret_name(seg) {
                                return Err(err("artifact_hidden", 403, "Hidden or restricted file."));
                            }
                        }
                        return Ok(ResolvedPath {
                            abs: p.join(candidate.file_name().unwrap_or_default()),
                            rel,
                        });
                    }
                    _ => return Err(err("artifact_path_not_found", 404, "Artifact path not found.")),
                }
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
            fs::create_dir_all(parent).map_err(|_| err("artifact_write_failed", 500, "Write failed."))?;
        }
        fs::write(&rp.abs, content).map_err(|_| err("artifact_write_failed", 500, "Write failed."))?;
        Ok(json!({ "saved": true, "path": rp.rel }))
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
