//! Phase 0: core/discovery.rs 에서 이관된 team tree 텍스트 파서 검증.
//! 파서(parse_team_tree)는 **구조만** 추출한다(역할/terminal 필터는 core 책임).
//! 따라서 여기서는 비터미널 surface 도 is_terminal=false 로 포함됨을 확인한다.

use agiteamapp_mux::parse_team_tree;

#[test]
fn parse_team_tree_extracts_structure() {
    let tree = concat!(
        "└── workspace ws:2 \"Panthea\" [selected] ◀ active\n",
        "    ├── surface surface:29 [terminal] \"제우스(PM)\" tty=ttys000\n",
        "    ├── surface surface:30 [terminal] \"불칸(BE)\" tty=ttys001\n",
        "    └── surface surface:31 [panel] \"메모(QA)\"\n",
    );
    let ws = parse_team_tree(tree);
    assert_eq!(ws.len(), 1);
    let w = &ws[0];
    assert_eq!(w.workspace_id, "ws:2");
    assert_eq!(w.title, "Panthea");
    assert!(w.selected);
    // 구조 추출: 비터미널(panel) 포함, is_terminal 플래그로 구분.
    assert_eq!(w.surfaces.len(), 3);
    let pm = w.surfaces.iter().find(|s| s.surface_id == "surface:29").unwrap();
    assert_eq!(pm.title, "제우스(PM)");
    assert!(pm.is_terminal);
    let qa = w.surfaces.iter().find(|s| s.surface_id == "surface:31").unwrap();
    assert!(!qa.is_terminal); // panel → 비터미널(core 가 제외)
}

#[test]
fn parse_team_tree_empty_input() {
    assert!(parse_team_tree("").is_empty());
}

#[test]
fn parse_team_tree_surface_before_workspace_ignored() {
    // workspace 라인 이전의 surface 는 소속 없음 → 무시(현행 cur_proj 가드 동등).
    let tree = "    ├── surface surface:9 [terminal] \"고아(PM)\"\n";
    assert!(parse_team_tree(tree).is_empty());
}
