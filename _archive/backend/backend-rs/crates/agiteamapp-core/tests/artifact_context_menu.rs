//! WG-ART-08/09 (DS-132) 새파일 생성·업로드 서비스 경계 검증.
//! Python tests/test_artifact_context_menu.py 와 동일 parity fixture(§9) 대조 기준.

use std::fs;
use std::path::PathBuf;

use agiteamapp_core::ArtifactService;
use unicode_normalization::UnicodeNormalization;

fn nfd(s: &str) -> String {
    s.nfd().collect()
}
fn nfc(s: &str) -> String {
    s.nfc().collect()
}

const PNG_1X1: &[u8] = &[
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
    0x42, 0x60, 0x82,
];

fn temp_root(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("agiteamapp-ctx-{}-{}", std::process::id(), tag));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(base.join("02.설계")).unwrap();
    base
}

fn svc(root: &PathBuf) -> ArtifactService {
    ArtifactService::new(root.clone(), "documents/".into())
}

// --- WG-ART-08 새파일 생성 -------------------------------------------------

#[test]
fn create_new_md() {
    let root = temp_root("create-md");
    let s = svc(&root);
    let out = s.create_file(Some("02.설계"), "새문서.md", "empty", "error", "documents", 1_048_576).unwrap();
    assert_eq!(out["file"]["path"], serde_json::json!("02.설계/새문서.md"));
    assert_eq!(out["file"]["extension"], serde_json::json!("md"));
    assert_eq!(out["file"]["render_mode"], serde_json::json!("markdown"));
    assert_eq!(out["file"]["content"], serde_json::json!(""));
    assert_eq!(out["tree_refresh"]["change_type"], serde_json::json!("created"));
    assert_eq!(out["tree_refresh"]["changed_path"], serde_json::json!("02.설계/새문서.md"));
    assert!(root.join("02.설계/새문서.md").exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_markdown_basic_and_json_templates() {
    let root = temp_root("create-tpl");
    let s = svc(&root);
    let md = s.create_file(Some("02.설계"), "노트.md", "markdown_basic", "error", "documents", 1_048_576).unwrap();
    assert_eq!(md["file"]["content"], serde_json::json!("# 노트\n"));
    s.create_file(Some("02.설계"), "data.json", "json_object", "error", "documents", 1_048_576).unwrap();
    assert_eq!(fs::read_to_string(root.join("02.설계/data.json")).unwrap(), "{}\n");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_json_template_mismatch_422() {
    let root = temp_root("create-tplmismatch");
    let e = svc(&root).create_file(Some("02.설계"), "x.md", "json_object", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "invalid_artifact_template");
    assert_eq!(e.http, 422);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_duplicate_error_409() {
    let root = temp_root("create-dup-err");
    let s = svc(&root);
    s.create_file(Some("02.설계"), "dup.md", "empty", "error", "documents", 1_048_576).unwrap();
    let e = s.create_file(Some("02.설계"), "dup.md", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "artifact_already_exists");
    assert_eq!(e.http, 409);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_duplicate_rename() {
    let root = temp_root("create-dup-rn");
    let s = svc(&root);
    s.create_file(Some("02.설계"), "dup.md", "empty", "error", "documents", 1_048_576).unwrap();
    let out = s.create_file(Some("02.설계"), "dup.md", "empty", "rename", "documents", 1_048_576).unwrap();
    assert_eq!(out["file"]["name"], serde_json::json!("dup (1).md"));
    assert!(root.join("02.설계/dup (1).md").exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_traversal_403() {
    let root = temp_root("create-trav");
    let e = svc(&root).create_file(Some(".."), "evil.md", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "path_forbidden");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_traversal_in_filename_invalid_path() {
    let root = temp_root("create-travname");
    let e = svc(&root).create_file(Some("02.설계"), "../evil.md", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "invalid_path");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_dotenv_hidden_403() {
    let root = temp_root("create-env");
    let e = svc(&root).create_file(Some("02.설계"), ".env", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "artifact_hidden");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_html_rejected_415() {
    let root = temp_root("create-html");
    let e = svc(&root).create_file(Some("02.설계"), "page.html", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "unsupported_media_type");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_parent_is_file_422() {
    let root = temp_root("create-pf");
    fs::write(root.join("02.설계/notes.txt"), "x").unwrap();
    let e = svc(&root).create_file(Some("02.설계/notes.txt"), "x.md", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "not_directory");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_parent_missing_404() {
    let root = temp_root("create-pm");
    let e = svc(&root).create_file(Some("없는폴더"), "x.md", "empty", "error", "documents", 1_048_576).unwrap_err();
    assert_eq!(e.code, "artifact_path_not_found");
    let _ = fs::remove_dir_all(&root);
}

// --- WG-ART-09 업로드 -------------------------------------------------------

#[test]
fn upload_png_valid_signature() {
    let root = temp_root("up-png");
    let out = svc(&root)
        .upload_file(Some("02.설계"), "diagram.png", PNG_1X1, "rename", "documents", None, 26_214_400, 1_048_576)
        .unwrap();
    assert_eq!(out["file"]["render_mode"], serde_json::json!("image"));
    assert_eq!(out["file"]["mime_type"], serde_json::json!("image/png"));
    assert!(out["file"]["stream_url"].is_string());
    assert_eq!(out["upload"]["size_bytes"], serde_json::json!(PNG_1X1.len()));
    assert!(out["upload"]["sha256"].is_string());
    assert_eq!(out["tree_refresh"]["changed_path"], serde_json::json!("02.설계/diagram.png"));
    assert_eq!(fs::read(root.join("02.설계/diagram.png")).unwrap(), PNG_1X1);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_fake_png_text_415() {
    let root = temp_root("up-fakepng");
    let e = svc(&root)
        .upload_file(Some("02.설계"), "fake.png", b"not a png", "rename", "documents", None, 26_214_400, 1_048_576)
        .unwrap_err();
    assert_eq!(e.code, "unsupported_media_type");
    assert_eq!(e.http, 415);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_too_large_413() {
    let root = temp_root("up-big");
    let big = vec![b'a'; 1025];
    let e = svc(&root)
        .upload_file(Some("02.설계"), "big.txt", &big, "rename", "documents", None, 1024, 1_048_576)
        .unwrap_err();
    assert_eq!(e.code, "file_too_large");
    assert_eq!(e.http, 413);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_binary_to_system_415() {
    let root = temp_root("up-sysbin");
    fs::create_dir_all(root.join("sub")).unwrap();
    let s = ArtifactService::new(root.clone(), "system/".into());
    let e = s
        .upload_file(Some("sub"), "diagram.png", PNG_1X1, "rename", "system", None, 26_214_400, 1_048_576)
        .unwrap_err();
    assert_eq!(e.code, "unsupported_media_type");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_text_to_system_ok() {
    let root = temp_root("up-systxt");
    let s = ArtifactService::new(root.clone(), "system/".into());
    let out = s
        .upload_file(Some(""), "note.py", b"print('hi')\n", "rename", "system", None, 26_214_400, 1_048_576)
        .unwrap();
    assert_eq!(out["file"]["render_mode"], serde_json::json!("code"));
    assert!(root.join("note.py").exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_invalid_utf8_text_422() {
    let root = temp_root("up-badutf8");
    let e = svc(&root)
        .upload_file(Some("02.설계"), "bad.txt", &[0xFF, 0xFE, 0x00, b'x'], "rename", "documents", None, 26_214_400, 1_048_576)
        .unwrap_err();
    assert_eq!(e.code, "invalid_text_encoding");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_duplicate_rename_and_error() {
    let root = temp_root("up-dup");
    let s = svc(&root);
    s.upload_file(Some("02.설계"), "diagram.png", PNG_1X1, "rename", "documents", None, 26_214_400, 1_048_576).unwrap();
    let out = s.upload_file(Some("02.설계"), "diagram.png", PNG_1X1, "rename", "documents", None, 26_214_400, 1_048_576).unwrap();
    assert_eq!(out["file"]["name"], serde_json::json!("diagram (1).png"));
    let e = s.upload_file(Some("02.설계"), "diagram.png", PNG_1X1, "error", "documents", None, 26_214_400, 1_048_576).unwrap_err();
    assert_eq!(e.code, "artifact_already_exists");
    let _ = fs::remove_dir_all(&root);
}

// --- QI-WG-048: NFD 한글 파일명 NFC 정규화 (Python parity) -----------------

#[test]
fn create_nfd_korean_normalized_to_nfc() {
    let root = temp_root("nfd-create");
    let s = svc(&root);
    let nfd_name = nfd("한글파일.md"); // 분해형 입력
    let out = s.create_file(Some("02.설계"), &nfd_name, "empty", "error", "documents", 1_048_576).unwrap();
    let name = out["file"]["name"].as_str().unwrap();
    let path = out["file"]["path"].as_str().unwrap();
    // 응답명/경로는 NFC(결합형)여야 한다.
    assert_eq!(name, nfc("한글파일.md"));
    assert_eq!(name, "한글파일.md");
    assert_eq!(path, "02.설계/한글파일.md");
    assert_eq!(out["tree_refresh"]["changed_path"], serde_json::json!("02.설계/한글파일.md"));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn create_nfd_duplicate_rename_normalized() {
    let root = temp_root("nfd-dup");
    let s = svc(&root);
    let nfd_name = nfd("한글파일.md");
    s.create_file(Some("02.설계"), &nfd_name, "empty", "error", "documents", 1_048_576).unwrap();
    let out = s.create_file(Some("02.설계"), &nfd_name, "empty", "rename", "documents", 1_048_576).unwrap();
    // 충돌 회피명도 NFC. (B2 라이브 케이스 정합)
    assert_eq!(out["file"]["name"], serde_json::json!("한글파일 (1).md"));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn upload_nfd_korean_normalized_to_nfc() {
    let root = temp_root("nfd-upload");
    let s = svc(&root);
    let nfd_name = nfd("업로드한글.txt");
    let out = s
        .upload_file(Some("02.설계"), &nfd_name, b"hi\n", "rename", "documents", None, 26_214_400, 1_048_576)
        .unwrap();
    // upload.filename / file.name 모두 NFC. (B4 라이브 케이스 정합)
    assert_eq!(out["upload"]["filename"], serde_json::json!("업로드한글.txt"));
    assert_eq!(out["file"]["name"], serde_json::json!("업로드한글.txt"));
    assert_eq!(out["upload"]["filename"].as_str().unwrap(), nfc("업로드한글.txt"));
    assert!(root.join("02.설계/업로드한글.txt").exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn resolve_nfd_path_matches_nfc_stored() {
    // NFD 경로로 resolve 시 NFC 로 통일되어 NFC 저장 파일과 매칭(다운로드/조회 기반).
    let root = temp_root("nfd-resolve");
    let s = svc(&root);
    s.create_file(Some("02.설계"), &nfd("문서.md"), "empty", "error", "documents", 1_048_576).unwrap();
    // NFD 경로로 read_file → 존재 매칭 + 이름 NFC.
    let (data, status) = s.read_file(&("02.설계/".to_string() + &nfd("문서.md")), true, 1_048_576, Some("documents")).unwrap();
    assert_eq!(status, 200);
    assert_eq!(data["file"]["name"], serde_json::json!("문서.md"));
    let _ = fs::remove_dir_all(&root);
}

// --- 다운로드명 검증 (validate_filename) -----------------------------------

#[test]
fn validate_filename_nfc() {
    let root = temp_root("vf-nfc");
    let s = svc(&root);
    // NFD 입력 → NFC 반환.
    assert_eq!(s.validate_filename(&nfd("한글.md")).unwrap(), nfc("한글.md"));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn validate_filename_rules() {
    let root = temp_root("vf");
    let s = svc(&root);
    assert!(s.validate_filename("정상.md").is_ok());
    assert_eq!(s.validate_filename("a/b.md").unwrap_err().code, "invalid_path");
    assert_eq!(s.validate_filename(".env").unwrap_err().code, "artifact_hidden");
    assert_eq!(s.validate_filename("  ").unwrap_err().code, "invalid_request");
    let _ = fs::remove_dir_all(&root);
}
