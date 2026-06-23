// 폴백(오프라인/목업) 데이터셋 — FE 도메인 모델 형태(어댑터 출력과 동일 shape).
// 백엔드(WG-PROJ-01 등) 연결 실패 시 store 가 이 데이터로 화면을 구성한다.
// 제우스 지시: "화면·라우팅·컴포넌트 구조부터" → 백엔드 없이도 UI 가 동작/시연 가능해야 함.
// 식별은 (projectId, role). surface 는 어디에도 담지 않는다.

export const MOCK_PROJECTS = [
  { projectId: "Panthea", title: "Panthea", connected: true, pmConnected: true, roomCount: 7 },
];

export const MOCK_ROOMS = {
  Panthea: [
    { roomId: "r-pm", projectId: "Panthea", role: "PM", roomType: "pm", displayName: "제우스", mono: "제", connectionState: "connected", readyState: "ready", collectorState: "running", unread: 0, lastText: "DV-40 프론트 착수 지시", lastAt: null, isPM: true },
    { roomId: "r-arch", projectId: "Panthea", role: "Architect", roomType: "role", displayName: "아테나", mono: "아", connectionState: "connected", readyState: "ready", collectorState: "running", unread: 0, lastText: "DS-40 v0.8 보완 완료", lastAt: null, isPM: false },
    { roomId: "r-be", projectId: "Panthea", role: "DeveloperBE", roomType: "role", displayName: "불칸", mono: "불", connectionState: "connected", readyState: "ready", collectorState: "running", unread: 1, lastText: "API 응답 스키마 정정 중", lastAt: null, isPM: false },
    { roomId: "r-fe", projectId: "Panthea", role: "DeveloperFE", roomType: "role", displayName: "이리스", mono: "이", connectionState: "connected", readyState: "ready", collectorState: "running", unread: 0, lastText: "DV-40 화면 구현 진행", lastAt: null, isPM: false },
    { roomId: "r-design", projectId: "Panthea", role: "Designer", roomType: "role", displayName: "뮤즈", mono: "뮤", connectionState: "connected", readyState: "ready", collectorState: "running", unread: 0, lastText: "4차시안 이식본 전달", lastAt: null, isPM: false },
    { roomId: "r-qa", projectId: "Panthea", role: "QA", roomType: "role", displayName: "아르고스", mono: "아", connectionState: "connected", readyState: "ready", collectorState: "delayed", unread: 2, lastText: "테스트 케이스 질문", lastAt: null, isPM: false },
    { roomId: "r-ops", projectId: "Panthea", role: "DevOps", roomType: "role", displayName: "아틀라스", mono: "아", connectionState: "connected", readyState: "ready", collectorState: "running", unread: 0, lastText: "compose 점검 완료", lastAt: null, isPM: false },
  ],
};

// 메시지는 FE 도메인 shape(어댑터 출력과 동일). canonical/diagnostic/degraded 플래그 포함.
// 일부 본문에 의도적으로 ANSI·터미널 chrome 잔재를 넣어 FE 방어 정제(strip) 동작을 시연한다.
export const MOCK_MESSAGES = {
  "r-pm": [
    { messageId: "m1", roomId: "r-pm", role: "PM", direction: "inbound", source: "transcript", messageType: "assistant_message", canonical: true, diagnostic: false, text: "DV-40 모니터 프론트를 구현하라. 선행(퍼블리싱·백엔드) 완료됨.", status: "received", out: false, occurredAt: null },
    { messageId: "m2", roomId: "r-pm", role: "PM", direction: "outbound", source: "webgui", messageType: "user_message", canonical: true, diagnostic: false, text: "DV-40.1 셸·프로젝트 전환부터 진행합니다.", status: "sent", out: true, occurredAt: null },
    { messageId: "m3", roomId: "r-pm", role: "PM", direction: "inbound", source: "transcript", messageType: "assistant_message", canonical: true, diagnostic: false, text: "좋다. 진행 상태를 표로 정리한다 👍\n\n| 단계 | 상태 |\n|---|---|\n| DV-40.1 셸 | **완료** |\n| DV-40.2 채팅 렌더 | 진행 |\n\n- `(project_id, role)` 식별 유지\n- 팀원 방은 **읽기 전용**", status: "received", out: false, occurredAt: null },
    // ANSI 색상 + 터미널 푸터 chrome 잔재가 섞인 본문 → FE 가 strip 후 표시
    { messageId: "m4", roomId: "r-pm", role: "PM", direction: "inbound", source: "transcript", messageType: "assistant_message", canonical: true, diagnostic: false, text: "[32m현 시점 활성 작업은 모니터 대화 렌더 정정이다.[0m\nbypass permissions on (shift+tab to cycle)", status: "received", out: false, occurredAt: null },
    // read-screen 스냅샷 보강(degraded) 본문 → '보강' 배지 + 어두운 아바타
    { messageId: "m5", roomId: "r-pm", role: "PM", direction: "inbound", source: "read_screen", messageType: "status", canonical: false, diagnostic: true, degraded: true, text: "(수집 지연 중 스냅샷) PM 응답 대기…", status: "degraded", out: false, occurredAt: null },
  ],
  "r-be": [
    { messageId: "b1", roomId: "r-be", role: "DeveloperBE", direction: "outbound", source: "webgui", messageType: "user_message", canonical: true, diagnostic: false, text: "API 응답 `source` 필드 enum 확정됐나요?", status: "sent", out: true, occurredAt: null },
    { messageId: "b2", roomId: "r-be", role: "DeveloperBE", direction: "inbound", source: "transcript", messageType: "assistant_message", canonical: true, diagnostic: false, text: "수집기를 transcript/hook canonical 로 재작성 중입니다. `source` 는 `bridge|transcript|hook` 로 정리됩니다.", status: "received", out: false, occurredAt: null },
  ],
  "r-qa": [
    // 미매칭(unmatched): 발신과 연결되지 않은 수신
    { messageId: "q1", roomId: "r-qa", role: "QA", direction: "inbound", source: "transcript", messageType: "unmatched", canonical: true, diagnostic: false, unmatched: true, text: "경계값 처리 기준 확인 요청드립니다.", status: "unmatched", out: false, occurredAt: null },
  ],
};

export const MOCK_TREE_ROOT = {
  path: "",
  name: "AgiTeamApp",
  isDir: true,
  ext: null,
  hasChildren: true,
  renderable: false,
  children: [
    { path: "03.management", name: "03.management", isDir: true, ext: null, hasChildren: true, renderable: false, children: null },
    {
      path: "04.development", name: "04.development", isDir: true, ext: null, hasChildren: true, renderable: false,
      children: [
        {
          path: "04.development/02.설계", name: "02.설계", isDir: true, ext: null, hasChildren: true, renderable: false,
          children: [
            { path: "04.development/02.설계/DS-50_화면설계서/DS-50_화면설계서.md", name: "DS-50_화면설계서.md", isDir: false, ext: "md", sizeBytes: 4096, hasChildren: false, renderable: true, children: null },
            { path: "04.development/02.설계/DS-40_인터페이스명세서/DS-40_API명세서.md", name: "DS-40_API명세서.md", isDir: false, ext: "md", sizeBytes: 51200, hasChildren: false, renderable: true, children: null },
          ],
        },
      ],
    },
  ],
};

export const MOCK_FILE = {
  "04.development/02.설계/DS-50_화면설계서/DS-50_화면설계서.md": {
    path: "04.development/02.설계/DS-50_화면설계서/DS-50_화면설계서.md",
    name: "DS-50_화면설계서.md",
    ext: "md",
    mime: "text/markdown",
    sizeBytes: 4096,
    renderMode: "markdown",
    sanitized: true,
    warnings: [],
    content:
      "# DS-50 화면설계서 — AgiTeamApp WebGUI\n\n## 1. 화면 목록\n\n| 화면 ID | 화면명 | 기능 |\n|---|---|---|\n| S-01 | 메인 레이아웃 | 3분할 셸 |\n| S-02 | 채팅방 목록 | 방 전환 |\n| S-03 | 대화 뷰 | PM 송수신 / 팀원 관찰 |\n| S-04 | 산출물 브라우저 | 트리 탐색 |\n| S-05 | 산출물 뷰어 | md·pdf·pptx·docx |\n\n> 이것은 **오프라인 목업** 본문입니다. 백엔드 연결 시 실제 산출물이 렌더됩니다.\n",
  },
};

export const MOCK_TODAY = "2026년 6월 7일";
