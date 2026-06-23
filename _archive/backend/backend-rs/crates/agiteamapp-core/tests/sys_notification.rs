//! 시스템 알림 분류기 검증 (모니터: 시스템 텍스트를 유저 말풍선에서 분리).
//! lib.rs 의 기존(무관) 인라인 테스트 컴파일 결함과 분리하기 위해 통합 테스트로 둔다.

use agiteamapp_core::transcript::classify_system_notification as classify;

#[test]
fn pure_system_reminder_is_system() {
    let t = "<system-reminder>\nThis is a reminder. Do X.\n</system-reminder>";
    let d = classify(t).expect("should classify as system");
    assert!(d.contains("This is a reminder"));
    assert!(!d.contains("<system-reminder>"));
}

#[test]
fn pure_task_notification_is_system() {
    let t = "<task-notification>\nBackground task abc completed.\n</task-notification>";
    let d = classify(t).expect("system");
    assert!(d.contains("completed"));
    assert!(!d.contains("</task-notification>"));
}

#[test]
fn system_notification_marker_is_system() {
    let t = "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThe agent finished.";
    let d = classify(t).expect("system");
    assert!(d.contains("finished"));
}

#[test]
fn multiple_blocks_only_is_system() {
    let t = "<system-reminder>r1</system-reminder>\n<task-notification>n1</task-notification>";
    assert!(classify(t).is_some());
}

#[test]
fn mixed_with_real_user_text_preserved_as_user() {
    // 실제 유저 텍스트가 시스템 블록과 함께 있으면 유저 발화로 보존(None).
    let t = "<system-reminder>ctx</system-reminder>\n실제 유저가 친 질문입니다";
    assert!(classify(t).is_none());
}

#[test]
fn plain_user_text_is_not_system() {
    assert!(classify("그냥 평범한 유저 메시지").is_none());
}

#[test]
fn unclosed_block_still_system() {
    // close 태그 없는 잘린 블록도 시스템으로 취급.
    let t = "<task-notification>\nin-progress notice without close";
    assert!(classify(t).is_some());
}
