//! WG-ART-07 휴지통식 삭제 보안·동작 검증.
//! 통합 테스트로 두어 lib.rs 인라인 테스트(무관 결함)와 분리.

use std::fs;
use std::path::PathBuf;

use agiteamapp_core::ArtifactService;

const TS: &str = "20260611112440";

/// 고유 임시 루트 생성(외부 crate 없이 pid 로 충돌 회피).
fn temp_root(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("agiteamapp-del-{}-{}", std::process::id(), tag));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();
    base
}

fn svc(root: &PathBuf) -> ArtifactService {
    ArtifactService::new(root.clone(), "documents/".into())
}

#[test]
fn delete_archives_then_removes_original() {
    let root = temp_root("archive");
    fs::write(root.join("foo_v0.2.html"), "body").unwrap();
    let s = svc(&root);
    let out = s.delete_file("foo_v0.2.html", TS).expect("should archive+delete");
    assert_eq!(out["deleted"], serde_json::json!(true));
    assert_eq!(out["archived"], serde_json::json!(true));
    assert_eq!(out["permanent"], serde_json::json!(false));
    // 백업명: 확장자 보존 + 타임스탬프를 이름과 확장자 사이.
    assert_eq!(out["archive_path"], serde_json::json!("_archive/foo_v0.2_20260611112440.html"));
    assert!(!root.join("foo_v0.2.html").exists(), "원본 삭제됨");
    let backup = root.join("_archive/foo_v0.2_20260611112440.html");
    assert!(backup.exists(), "백업 생성됨");
    assert_eq!(fs::read_to_string(&backup).unwrap(), "body", "백업 내용 동일");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn archive_folder_auto_created() {
    let root = temp_root("autocreate");
    assert!(!root.join("_archive").exists());
    fs::write(root.join("a.md"), "x").unwrap();
    let s = svc(&root);
    s.delete_file("a.md", TS).unwrap();
    assert!(root.join("_archive").is_dir(), "_archive 자동 생성");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn nested_file_archived_in_same_folder() {
    let root = temp_root("nested");
    fs::create_dir_all(root.join("sub/dir")).unwrap();
    fs::write(root.join("sub/dir/a.md"), "x").unwrap();
    let s = svc(&root);
    let out = s.delete_file("sub/dir/a.md", TS).unwrap();
    assert_eq!(out["archive_path"], serde_json::json!("sub/dir/_archive/a_20260611112440.md"));
    assert!(root.join("sub/dir/_archive/a_20260611112440.md").exists());
    assert!(!root.join("sub/dir/a.md").exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn file_without_extension_archived() {
    let root = temp_root("noext");
    fs::write(root.join("Makefile"), "all:").unwrap();
    let s = svc(&root);
    let out = s.delete_file("Makefile", TS).unwrap();
    assert_eq!(out["archive_path"], serde_json::json!("_archive/Makefile_20260611112440"));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn delete_inside_archive_is_permanent_no_nested_backup() {
    let root = temp_root("inarchive");
    fs::create_dir_all(root.join("_archive")).unwrap();
    fs::write(root.join("_archive/old_20260101000000.md"), "old").unwrap();
    let s = svc(&root);
    let out = s.delete_file("_archive/old_20260101000000.md", TS).unwrap();
    assert_eq!(out["permanent"], serde_json::json!(true));
    assert_eq!(out["archived"], serde_json::json!(false));
    assert_eq!(out["archive_path"], serde_json::Value::Null);
    assert!(!root.join("_archive/old_20260101000000.md").exists(), "영구 삭제됨");
    // 중첩 _archive(_archive/_archive) 가 생기지 않아야 함.
    assert!(!root.join("_archive/_archive").exists(), "중첩 백업 없음");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn nested_archive_segment_anywhere_is_permanent() {
    let root = temp_root("archseg");
    fs::create_dir_all(root.join("docs/_archive")).unwrap();
    fs::write(root.join("docs/_archive/x.md"), "x").unwrap();
    let s = svc(&root);
    let out = s.delete_file("docs/_archive/x.md", TS).unwrap();
    assert_eq!(out["permanent"], serde_json::json!(true));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn copy_failure_preserves_original() {
    let root = temp_root("copyfail");
    fs::write(root.join("keep.md"), "important").unwrap();
    // 부모에 '_archive' 라는 '파일'을 만들어 create_dir_all(_archive) 를 실패시킨다.
    fs::write(root.join("_archive"), "i am a file not a dir").unwrap();
    let s = svc(&root);
    let err = s.delete_file("keep.md", TS).unwrap_err();
    assert_eq!(err.http, 500);
    assert_eq!(err.code, "artifact_archive_failed");
    assert!(root.join("keep.md").exists(), "백업 실패 시 원본 보존돼야 함");
    assert_eq!(fs::read_to_string(root.join("keep.md")).unwrap(), "important");
    let _ = fs::remove_dir_all(&root);
}

// --- 보안 가드 회귀 (휴지통식으로 바뀌어도 유지) ---

#[test]
fn delete_traversal_rejected_and_outside_file_safe() {
    let root = temp_root("traversal");
    let outside = root.parent().unwrap().join("VICTIM.txt");
    fs::write(&outside, "must survive").unwrap();
    let s = svc(&root);
    let err = s.delete_file("../VICTIM.txt", TS).unwrap_err();
    assert_eq!(err.http, 403);
    assert_eq!(err.code, "path_forbidden");
    assert!(outside.exists(), "루트 밖 파일은 삭제 금지");
    let _ = fs::remove_file(&outside);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn delete_absolute_path_rejected() {
    let root = temp_root("abs");
    let s = svc(&root);
    assert_eq!(s.delete_file("/etc/hosts", TS).unwrap_err().http, 403);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn delete_missing_file_404() {
    let root = temp_root("missing");
    let s = svc(&root);
    let err = s.delete_file("nope.md", TS).unwrap_err();
    assert_eq!(err.http, 404);
    assert_eq!(err.code, "artifact_path_not_found");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn delete_directory_rejected() {
    let root = temp_root("isdir");
    fs::create_dir_all(root.join("folder")).unwrap();
    let s = svc(&root);
    assert_eq!(s.delete_file("folder", TS).unwrap_err().http, 422);
    assert!(root.join("folder").exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn delete_root_itself_rejected() {
    let root = temp_root("rootself");
    let s = svc(&root);
    assert_eq!(s.delete_file("", TS).unwrap_err().http, 403);
    let _ = fs::remove_dir_all(&root);
}
