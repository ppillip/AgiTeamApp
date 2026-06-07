// 무의존 자체 테스트 (node 실행). 변경 핵심 순수 모듈을 검증한다.
//   node tests/selftest.mjs
// 대상: src/lib/markdown.js, src/api/adapters.js (둘 다 import.meta 미사용 → node 로딩 가능)
import { renderMarkdown } from "../src/lib/markdown.js";
import {
  adaptProjects,
  adaptRooms,
  adaptMessages,
  adaptNode,
  adaptFile,
  roleLabel,
  roleOrder,
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

// ── adapters: messages (방향→out) ───────────────────────────
const msgs = adaptMessages([
  { message_id: "m1", room_id: "r", role: "PM", direction: "outbound", source: "webgui", message_type: "user_message", text: "보냄", status: "sent" },
  { message_id: "m2", room_id: "r", role_id: "Architect", direction: "inbound", source: "role_log", message_type: "log_line", text: "받음", status: "received" },
]);
ok(msgs[0].out === true && msgs[1].out === false, "msg: direction→out");
ok(msgs[1].role === "Architect", "msg: role_id 흡수");
ok(msgs[1].source === "role_log" && msgs[1].messageType === "log_line", "msg: source/type 확정 스키마");

// ── adapters: node / file ───────────────────────────────────
const node = adaptNode({ path: "a", name: "a", node_type: "directory", has_children: true, children: [{ path: "a/b.md", name: "b.md", node_type: "file", extension: "md", renderable: true }] });
ok(node.isDir && node.children[0].ext === "md" && node.children[0].renderable, "node: 트리 변환");

const file = adaptFile({ path: "x.md", name: "x.md", extension: "md", mime_type: "text/markdown", size_bytes: 10, render_mode: "markdown", content: "# x", sanitized: true, render_warnings: [] });
ok(file.renderMode === "markdown" && file.content === "# x", "file: 메타 변환");

ok(roleLabel("DeveloperFE") === "FE", "label: FE 약어");
ok(roleOrder("PM") === 0 && roleOrder("DevOps") === 6, "order: 역할 순서");

// ── 결과 ────────────────────────────────────────────────────
console.log(`\nselftest: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
