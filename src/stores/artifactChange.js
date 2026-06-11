// 산출물 변경(artifact_changed / WG-ART-04) 매핑 순수 로직 (DV-71).
// 부수효과·프레임워크 의존 없음 → node 단위 테스트(tests/selftest.mjs)로 직접 검증 가능.
// 설계 근거: DS-40 §10.3/§10.4, DS-60 §8.4.
//
// 매핑 원칙(DS-40 §10.4):
//  - 열린 트리 노드의 부모 디렉토리(parent_path)가 변경되면 그 디렉토리만 WG-ART-01 재요청.
//  - 현재 뷰어 중인 파일이 modified/created 면 WG-ART-02 재요청, deleted 면 not-found 안내.
//  - 열지 않은 노드/표시 중이지 않은 파일 변경은 즉시 재요청하지 않는다(다음 펼침/열람 때 최신).

export function parentOf(path) {
  if (!path) return "";
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

// 변경 1건을 받아 "무엇을 해야 하는지" 계획만 반환(실행은 store 가 담당).
//
// data: artifact_changed 의 data 모델 { project_id, change_type|kind, path, parent_path, node_type, ... }
// view: 현재 화면 상태 스냅샷
//   { selectedProjectId, viewerOpen, viewerPath, expanded }  (expanded: { [path]: boolean })
//
// 반환 plan:
//   { ignore: true }                         // 무시(잘못된 입력·타 프로젝트)
//   {
//     ignore: false,
//     path, parent, changeType,
//     viewer: "reload" | "deleted" | null,   // 현재 뷰어 파일 처리
//     purge: boolean,                          // 삭제된 디렉토리 펼침/캐시 정리 여부
//     refreshDir: "" | "<dirPath>" | null,    // 재요청 디렉토리("" = 루트, null = 보이지 않음→skip)
//   }
export function planArtifactChange(data, view) {
  if (!data || !data.path) return { ignore: true };
  const v = view || {};
  if (data.project_id && v.selectedProjectId && data.project_id !== v.selectedProjectId) {
    return { ignore: true };
  }
  const path = data.path;
  const changeType = data.change_type || data.kind || null; // BE 는 change_type, kind 표기도 관용 수용
  const parent = data.parent_path != null ? data.parent_path : parentOf(path);

  let viewer = null;
  if (v.viewerOpen && v.viewerPath === path) {
    viewer = changeType === "deleted" ? "deleted" : "reload";
  }

  const purge = changeType === "deleted";

  const isRoot = parent === "" || parent == null;
  const expanded = v.expanded || {};
  const refreshDir = isRoot ? "" : expanded[parent] ? parent : null;

  return { ignore: false, path, parent, changeType, viewer, purge, refreshDir };
}
