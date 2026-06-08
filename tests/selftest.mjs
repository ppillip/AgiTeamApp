// 무의존 자체 테스트 (node 실행). 변경 핵심 순수 모듈을 검증한다.
//   node tests/selftest.mjs
// 대상: src/lib/markdown.js, src/api/adapters.js (둘 다 import.meta 미사용 → node 로딩 가능)
import { renderMarkdown } from "../src/lib/markdown.js";
import {
  stripAnsi,
  stripTerminalChrome,
  normalizeWhitespace,
  cleanMessageText,
  renderMessageBody,
} from "../src/lib/sanitize.js";
import {
  adaptProjects,
  adaptRooms,
  adaptMessages,
  adaptNode,
  adaptFile,
  roleLabel,
  roleOrder,
  provenanceInfo,
  connectionInfo,
} from "../src/api/adapters.js";

let pass = 0;
let fail = 0;
function ok(cond, label) {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error("  ✗ FAIL:", label);
  }
}

// ── markdown ────────────────────────────────────────────────
const h = renderMarkdown("# 제목\n\n본문 **굵게** 와 `코드`.");
ok(h.includes("<h1"), "md: h1 렌더");
ok(h.includes("<strong>굵게</strong>"), "md: bold");
ok(h.includes('<code class="md-code">코드</code>'), "md: inline code");

const tbl = renderMarkdown("| A | B |\n|---|---|\n| 1 | 2 |");
ok(tbl.includes("<table"), "md: 테이블 렌더");
ok(tbl.includes("<th>A</th>") && tbl.includes("<td>1</td>"), "md: 테이블 셀");

const xss = renderMarkdown('<img src=x onerror="alert(1)">');
ok(!xss.includes("<img"), "md: raw HTML escape(XSS 차단)");
ok(xss.includes("&lt;img"), "md: escape 확인");

const link = renderMarkdown("[클릭](javascript:alert(1))");
ok(!link.includes("javascript:"), "md: javascript 링크 차단");
const link2 = renderMarkdown("[문서](https://x.io)");
ok(link2.includes('href="https://x.io"'), "md: 정상 링크 허용");

const code = renderMarkdown("```js\nconst a=1;\n```");
ok(code.includes("<pre") && code.includes("const a=1;"), "md: 코드펜스");

// ── adapters: projects (registry형) ─────────────────────────
const projReg = adaptProjects({
  projects: [
    {
      project_id: "Panthea",
      workspace_id: "workspace:6",
      connected: true,
      roles: [
        { role_id: "PM", display_name: "제우스", connection_state: "connected" },
        { role_id: "DeveloperFE", display_name: "이리스", connection_state: "connected" },
      ],
    },
  ],
});
ok(projReg.projects[0].projectId === "Panthea", "proj: project_id");
ok(projReg.projects[0].pmConnected === true, "proj: pmConnected(roles 기반)");
ok(projReg.selectedProjectId === "Panthea", "proj: selected 기본값");

// ── adapters: projects (ProjectSummary형) ───────────────────
const projSum = adaptProjects({
  projects: [{ project_id: "X", workspace_title: "엑스", connection_state: "connected", pm_connection_state: "connected", room_count: 7 }],
  selected_project_id: "X",
});
ok(projSum.projects[0].title === "엑스" && projSum.projects[0].roomCount === 7, "proj: ProjectSummary형 흡수");

// ── adapters: rooms (role vs role_id 흡수 + 정렬) ───────────
const rooms = adaptRooms({
  rooms: [
    { room_id: "r2", project_id: "P", role_id: "QA", display_name: "아르고스", room_type: "role", unread_count: 2, connection_state: "disconnected" },
    { room_id: "r1", project_id: "P", role: "PM", display_name: "제우스", room_type: "pm", connection_state: "connected" },
  ],
});
ok(rooms[0].role === "PM", "room: 역할 순서 정렬(PM 먼저)");
ok(rooms[0].isPM === true && rooms[1].isPM === false, "room: isPM 판정");
ok(rooms[1].unread === 2, "room: unread");
ok(rooms.every((r) => !("surface_id" in r) && !("surfaceId" in r)), "room: surface 미노출");

// ── sanitize: ANSI / 터미널 chrome / 공백 방어 (DV-40 / DS-60 §6.5·§10.2) ──
ok(stripAnsi("\x1b[31m빨강\x1b[0m") === "빨강", "san: ANSI CSI 색상 strip");
ok(stripAnsi("\x1b]0;제목\x07본문") === "본문", "san: ANSI OSC title strip");
ok(stripAnsi("]0;some/title\x07진짜본문") === "진짜본문", "san: ESC 유실 OSC 잔재 strip (]0;)");
ok(!/\x1b/.test(stripAnsi("\x1b[2J\x1b[H지움")), "san: 화면지움/커서 시퀀스 strip");
ok(stripAnsi("탭\t유지\n줄바꿈유지") === "탭\t유지\n줄바꿈유지", "san: 탭·개행 보존");
// ESC 유실 orphan ANSI 조각 (실 백엔드 관측: [>4;2m, [?2026h, ]0;, [2C, ]10;?\)
ok(stripAnsi("본문[>4;2m뒤") === "본문뒤", "san: orphan private CSI([>4;2m) strip");
ok(stripAnsi("a[?2026h b") === "a b", "san: orphan private CSI([?2026h) strip");
ok(stripAnsi("x?2026h y") === "x y", "san: ESC·[ 유실 ?2026h strip");
ok(stripAnsi("[0m색[32m상[0m") === "색상", "san: orphan SGR 색상 strip");
ok(stripAnsi("이동[2C후[1A끝") === "이동후끝", "san: orphan 커서이동([2C,[1A) strip");
ok(stripAnsi("종료]10;?\\") === "종료", "san: orphan OSC([10;?) strip");
// 마크다운/프로즈 보존 — orphan strip 이 정상 문법을 먹지 않아야 함
ok(stripAnsi("[문서](https://x.io) 참고 [1] 각주") === "[문서](https://x.io) 참고 [1] 각주", "san: 마크다운 링크·각주 보존");
ok(stripAnsi("배열 arr[0] 과 map[key]") === "배열 arr[0] 과 map[key]", "san: 코드성 대괄호 보존");
ok(
  stripTerminalChrome("실내용\nbypass permissions on (shift+tab to cycle)\n끝") === "실내용\n끝",
  "san: 터미널 푸터(bypass permissions) 라인 제거"
);
ok(
  stripTerminalChrome("본문\n? for shortcuts\n끝") === "본문\n끝",
  "san: 단축키 안내 chrome 라인 제거"
);
// 공백 소실로 본문과 한 줄에 붙은 푸터(인라인 스크럽)
ok(
  !/shift|cycle|bypass/i.test(
    stripTerminalChrome("작업진행중⏵⏵bypasspermissionson (shift+tabtocycle)·esctointerrupt")
  ),
  "san: 공백소실·인라인 푸터 스크럽(bypass/shift+tab/esc)"
);
ok(
  stripTerminalChrome("실제 내용 ⏵⏵ bypass permissions on (shift+tab to cycle)").includes("실제 내용") &&
    !/cycle/i.test(stripTerminalChrome("실제 내용 ⏵⏵ bypass permissions on (shift+tab to cycle)")),
  "san: 인라인 푸터 제거 후 본문 보존"
);
// cmux 작업바 변종(shift+tab 미동반, ·N shell·ctrl+t to hide tasks)
ok(
  !/bypass|shell|hide tasks/i.test(
    stripTerminalChrome("결과 보고드립니다 ⏵⏵bypasspermissionson ·1shell ·ctrl+ttohidetasks·")
  ),
  "san: cmux 작업바 푸터 변종 스크럽"
);
ok(
  stripTerminalChrome("결과 보고드립니다 ⏵⏵bypasspermissionson ·1shell ·ctrl+ttohidetasks·").includes("결과 보고드립니다"),
  "san: cmux 작업바 변종 제거 후 본문 보존"
);
ok(normalizeWhitespace("a b  c") === "a b  c", "san: 단어 사이 공백 보존(소실 방지)");
ok(normalizeWhitespace("줄끝공백   \n다음") === "줄끝공백\n다음", "san: 줄끝 공백만 제거");
ok(normalizeWhitespace("a\n\n\n\nb") === "a\n\nb", "san: 과다 빈줄 축약");
ok(
  cleanMessageText("\x1b[32m현 시점 활성 작업\x1b[0m\nbypass permissions on (shift+tab to cycle)") ===
    "현 시점 활성 작업",
  "san: ANSI+chrome 동시 제거, 본문 공백 보존"
);
const body = renderMessageBody("## 제목 😀\n\n| A | B |\n|---|---|\n| 1 | 2 |");
ok(body.includes("<h2") && body.includes("<table") && body.includes("😀"), "san: 정제 후 마크다운(표·헤더·이모지) 렌더");
const bodyXss = renderMessageBody('<script>alert(1)</script>');
ok(!bodyXss.includes("<script>") && bodyXss.includes("&lt;script&gt;"), "san: 본문 XSS escape");

// ── adapters: messages (방향→out + degraded/diagnostic 플래그) ───────────
const msgs = adaptMessages([
  { message_id: "m1", room_id: "r", role: "PM", direction: "outbound", source: "webgui", message_type: "user_message", text: "보냄", status: "sent" },
  { message_id: "m2", room_id: "r", role_id: "Architect", direction: "inbound", source: "role_log", message_type: "log_line", text: "받음", status: "received" },
  { message_id: "m3", room_id: "r", role: "PM", direction: "inbound", source: "transcript", message_type: "assistant_message", text: "canonical", status: "received" },
  { message_id: "m4", room_id: "r", role: "PM", direction: "inbound", source: "read_screen", message_type: "status", text: "스냅샷", status: "degraded" },
  { message_id: "m5", room_id: "r", role: "PM", direction: "inbound", source: "transcript", message_type: "unmatched", text: "미매칭", status: "unmatched" },
]);
ok(msgs[0].out === true && msgs[1].out === false, "msg: direction→out");
ok(msgs[1].role === "Architect", "msg: role_id 흡수");
ok(msgs[1].source === "role_log" && msgs[1].messageType === "log_line", "msg: 구 source/type passthrough(하위호환)");
ok(msgs[2].canonical === true && msgs[2].diagnostic === false, "msg: transcript canonical 판정");
ok(msgs[1].diagnostic === true, "msg: role_log 진단 출처 판정");
ok(msgs[3].degraded === true && msgs[3].diagnostic === true, "msg: read_screen degraded 판정");
ok(msgs[4].unmatched === true, "msg: unmatched 판정");

// ── adapters: out 보강 (방향 누락 시 message_type 으로 질문/답변 좌우 판정) ──
// WS 희소 페이로드 방어: direction 없이도 user_message(질문)=우측, assistant=좌측.
const sparse = adaptMessages([
  { message_id: "s1", room_id: "r", message_type: "user_message", text: "질문", status: "sent" },
  { message_id: "s2", room_id: "r", message_type: "assistant_message", text: "답변", status: "received" },
  { message_id: "s3", room_id: "r", direction: "inbound", message_type: "user_message", text: "방향우선", status: "received" },
]);
ok(sparse[0].out === true, "msg: 방향누락+user_message → out(우측)");
ok(sparse[1].out === false, "msg: 방향누락+assistant_message → out=false(좌측)");
ok(sparse[2].out === false, "msg: 명시 direction(inbound) 이 message_type 보강보다 우선");

// ── adapters: node / file ───────────────────────────────────
const node = adaptNode({ path: "a", name: "a", node_type: "directory", has_children: true, children: [{ path: "a/b.md", name: "b.md", node_type: "file", extension: "md", renderable: true }] });
ok(node.isDir && node.children[0].ext === "md" && node.children[0].renderable, "node: 트리 변환");

const file = adaptFile({ path: "x.md", name: "x.md", extension: "md", mime_type: "text/markdown", size_bytes: 10, render_mode: "markdown", content: "# x", sanitized: true, render_warnings: [] });
ok(file.renderMode === "markdown" && file.content === "# x", "file: 메타 변환");

ok(roleLabel("DeveloperFE") === "FE", "label: FE 약어");
ok(roleOrder("PM") === 0 && roleOrder("DevOps") === 6, "order: 역할 순서");

// ── provenance / connection 표식 (DV-44 / DS-60 §4.4·§6.1) ──────────
ok(provenanceInfo("hook").label === "LIVE HOOK" && provenanceInfo("hook").real === true, "prov: hook=LIVE HOOK 실데이터");
ok(provenanceInfo("transcript").label === "LIVE TRANSCRIPT" && provenanceInfo("transcript").real, "prov: transcript=LIVE TRANSCRIPT 실데이터");
ok(provenanceInfo("webgui").label === "SENT" && provenanceInfo("webgui").real, "prov: webgui=SENT");
ok(provenanceInfo("manual").label === "MANUAL" && provenanceInfo("manual").real === false, "prov: manual=MANUAL 비실데이터(실 hook 위장 금지)");
ok(provenanceInfo("mock").label === "MOCK" && !provenanceInfo("mock").real, "prov: mock=MOCK");
ok(provenanceInfo("transcript", { isMock: true }).label === "MOCK", "prov: isMock 우선 → MOCK");
ok(provenanceInfo("read_screen").label === "DIAGNOSTIC", "prov: read_screen=DIAGNOSTIC");
ok(provenanceInfo("zzz").label === null, "prov: 미지 source → 표식 없음");

ok(connectionInfo("connected", "live").label === "LIVE", "conn: connected/live=LIVE");
ok(connectionInfo("disconnected", "disconnected").label === "끊김", "conn: disconnected=끊김");
ok(connectionInfo("unknown", "mock").label === "MOCK", "conn: runtime_state=mock → MOCK");
ok(connectionInfo("disconnected", "disconnected", { mock: true }).label === "MOCK", "conn: mock 플래그 우선");

// adaptMessage provenance 객체 흡수 + team_session_id
const provMsgs = adaptMessages([
  { message_id: "p1", room_id: "r", direction: "inbound", message_type: "assistant_message", text: "본문", status: "received", provenance: { source: "transcript", kind: "real", is_real_data: true }, team_session_id: "20260608_1" },
  { message_id: "p2", room_id: "r", direction: "inbound", message_type: "status", text: "목", status: "received", provenance: { source: "mock", kind: "mock", is_real_data: false } },
]);
ok(provMsgs[0].provLabel === "LIVE TRANSCRIPT" && provMsgs[0].provTone === "live" && provMsgs[0].isRealData === true, "msg: provenance transcript 흡수");
ok(provMsgs[0].teamSessionId === "20260608_1", "msg: team_session_id 흡수");
ok(provMsgs[1].provLabel === "MOCK" && provMsgs[1].isMock === true, "msg: provenance mock 흡수");

// adaptRoom provenance / runtime_state 흡수
const provRooms = adaptRooms({
  rooms: [
    { room_id: "rr", project_id: "P", role: "DeveloperBE", room_type: "role", connection_state: "disconnected", runtime_state: "disconnected", provenance: { source: "transcript", kind: "real", is_real_data: true } },
  ],
});
ok(provRooms[0].runtimeState === "disconnected" && provRooms[0].provSource === "transcript" && provRooms[0].isMock === false, "room: provenance/runtime_state 흡수");

// ── 결과 ────────────────────────────────────────────────────
console.log(`\nselftest: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
