//! WG-MSG-06 mux 제출 텍스트 합성 검증 (compose_submit_text).

use agiteamapp_core::send::compose_submit_text;

#[test]
fn no_attachments_returns_text_as_is() {
    assert_eq!(compose_submit_text("hello", &[]), "hello");
    assert_eq!(compose_submit_text("  trimmed  ", &[]), "trimmed");
}

#[test]
fn image_only_includes_path_block_no_empty_submit() {
    let out = compose_submit_text("", &["/abs/a.png".into()]);
    // 텍스트 없이도 경로 블록이 들어가 빈 submit 방지 + PM 이 절대경로를 Read 가능.
    assert_eq!(out, "[첨부 이미지 1개]\n/abs/a.png");
    assert!(!out.is_empty());
    assert!(out.contains("/abs/a.png"));
}

#[test]
fn text_plus_images_preserves_order() {
    let out = compose_submit_text(
        "보세요",
        &["/abs/1.png".into(), "/abs/2.jpg".into()],
    );
    assert_eq!(out, "보세요\n[첨부 이미지 2개]\n/abs/1.png\n/abs/2.jpg");
}
