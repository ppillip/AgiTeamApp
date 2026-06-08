// 대화 본문 방어적 정제 (DV-40 / DS-60 §6.5·§10.2 raw TUI 잔재 방어).
//
// BE 수집기가 transcript/hook canonical 본문으로 재작성 중이지만(불칸),
// 혹시 ANSI 이스케이프·터미널 chrome(푸터/스피너/OSC title)·제어문자 잔재가
// 말풍선 본문에 섞여 와도 FE 가 표시 직전에 제거한다(이중 방어).
// 정제 결과를 마크다운 렌더 입력으로 넘긴다.
//
// 설계 근거: DS-60 §6.5 "TUI raw stream(ANSI cursor/alternate screen/repaint)은
// 본문 수집원으로 사용하지 않는다", §10.2 "raw role log가 TUI ANSI여도 말풍선 미표시".
// FE 는 canonical 본문을 신뢰하되, 잔재가 새어 들어와도 화면이 깨지지 않게 방어한다.
//
// 공백 원칙: 단어 사이 공백 소실("현시점활성작업은모니터")은 BE 수집 단계 문제이며
// FE 에서 복원할 수 없다. FE 는 들어온 공백을 **보존**하고, 줄 끝 공백·과다 빈줄만
// 정규화한다(단어 사이 단일/다중 공백은 절대 합치지 않는다).

import { renderMarkdown } from "./markdown.js";

// ANSI CSI: ESC [ ... 최종바이트 (색상 \x1b[0m, 커서이동 \x1b[2J 등)
const ANSI_CSI = /\x1b\[[0-9;?<>=]*[ -/]*[@-~]/g;
// ANSI OSC: ESC ] ... (BEL \x07 또는 ST \x1b\\). 터미널 제목 ]0; 등.
const ANSI_OSC = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
// 기타 단문자 ESC 시퀀스 (\x1bN, \x1b= 등) + ESC P(DCS)/_(APC) 시작
const ANSI_OTHER = /\x1b[=>NOPc()#_^]/g;
// 남은 고립 ESC 문자
const LONE_ESC = /\x1b/g;
// 제어문자 (개행 \n, 탭 \t 은 보존)
const CTRL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

// ── ESC 바이트가 상위(터미널/수집)에서 유실되고 본문만 남은 ANSI 조각 ──
// 유저 실측: "]0; 누출", 그리고 실 백엔드에서 [>4;2m, [?2026h, [<u, [2C 등 관측.
// 보수적으로: 프로즈에 등장하지 않는 형태(private-mode 접두 <>=?, SGR 색상,
// 커서/화면 제어 최종바이트)만 제거한다. 마크다운 링크 [텍스트](url) 는 보존.
const ORPHAN = [
  /\][0-9]{1,2};[^\n\x07]*(?:\x07|(?=\n)|$)/g, // OSC title 잔재: ]0;...; ]2;...
  /\][0-9]{1,2};\?[\\]?/g, // ]10;?\  ]11;?\  류
  /\[[?<>=][0-9;]*[a-zA-Z]/g, // private CSI: [?2026h [>4;2m [<u [=...
  /\?[0-9]{3,4}[hl]\b/g, // [ 까지 유실: ?2026h ?25l
  /\[[0-9]{1,3}(?:;[0-9]{1,3})*m/g, // SGR 색상 잔재: [0m [32m [1;31m
  /\[[0-9]{1,3}(?:;[0-9]{1,3})*[A-HJKSTfsu]/g, // 커서/화면 제어: [2C [1A [2J [H
];

function stripOrphanAnsi(s) {
  let t = s;
  for (const re of ORPHAN) t = t.replace(re, "");
  return t;
}

// ANSI / OSC / orphan 조각 / 제어문자 제거.
// 주의: BEL(\x07)은 OSC 종결자이므로 orphan 처리까지 보존하고 CTRL 제거는 맨 마지막에 한다.
export function stripAnsi(s) {
  let t = String(s)
    .replace(ANSI_OSC, "")
    .replace(ANSI_CSI, "")
    .replace(ANSI_OTHER, "")
    .replace(LONE_ESC, "");
  t = stripOrphanAnsi(t); // ESC 유실 조각 (BEL 아직 살아있음)
  t = t.replace(CTRL, ""); // 남은 제어문자(BEL 포함) 최종 제거
  return t;
}

// 터미널 UI chrome(푸터·단축키 안내·스피너·인터럽트 안내) 라인 패턴.
// 팀 대화 본문에는 등장하지 않는 CLI TUI 고유 문구만 보수적으로 골라 제거한다.
// 전체-라인 전용 chrome 패턴(인라인 스크럽으로 다루지 않는 것만, 앵커 고정).
const CHROME_LINE = [
  /^\s*press\s+(up|enter)\s+to\s+.*$/i,
  /^\s*\d+%\s+context\s+(left|used).*$/i,
];

// 공백 비의존 인라인 chrome 스크럽.
// TUI repaint 에서 공백이 소실돼 글자가 붙은 푸터("bypasspermissionson(shift+tabtocycle)")가
// 다른 본문과 한 줄에 섞여 와도 제거한다. 토큰 사이를 \s* 로 둬 공백 유무 모두 매칭.
const CHROME_INLINE = [
  // bypass permissions [on] (… shift+tab to cycle) — 공백 소실/접두 화살표 변종 포함
  /[⏵▶►»]{0,2}\s*bypass\s*permissions?(?:\s*on)?[\s\S]{0,4}?shift\s*\+?\s*tab\s*to\s*cycle\s*\)?/gi,
  // bypass permissions [on] (shift+tab 미동반: cmux 작업바 변종 ·1 shell·ctrl+t…)
  /[⏵▶►»]{1,2}\s*(?:bypass\s*permissions?|accept\s*edits|auto-?accept\s*edits)(?:\s*on)?/gi,
  /\(?\s*shift\s*\+?\s*tab\s*to\s*cycle\s*\)?/gi,
  /\besc\s*to\s*interrupt/gi,
  /\?\s*for\s*shortcuts/gi,
  // cmux 작업바 / CLI 입력 hint chrome
  /ctrl\s*\+?\s*t\s*to\s*hide\s*tasks?/gi,
  /·?\s*\d+\s*shells?(?=[\s·•]|$)/gi,
  /image\s*in\s*clipboard/gi,
  /ctrl\s*\+?\s*v\s*to(?:\s*(?:paste|add))?/gi,
];

function scrubInlineChrome(s) {
  let t = s;
  for (const re of CHROME_INLINE) t = t.replace(re, " ");
  return t;
}

// 스크럽 후 의미 글자 없이 장식(공백·박스선·구분자)만 남았는지.
const DECOR_ONLY = /[\s│─━┄┈┃·•▪◦…\-=*_~❯❮>«»⏵▶►]+/g;

// 터미널 chrome 제거:
//  1) 앵커된 전체-라인 chrome 드롭
//  2) 나머지 라인은 인라인 스크럽(본문 보존)
//  3) 원래 내용이 있었으나 스크럽 결과 장식만 남은 라인은 드롭
export function stripTerminalChrome(s) {
  const out = [];
  for (const raw of String(s).split("\n")) {
    if (CHROME_LINE.some((re) => re.test(raw))) continue;
    const scrubbed = scrubInlineChrome(raw);
    const hadContent = raw.trim().length > 0;
    const meaningful = scrubbed.replace(DECOR_ONLY, "").length > 0;
    if (hadContent && !meaningful) continue; // 전부 chrome/장식이던 라인 드롭
    out.push(scrubbed);
  }
  return out.join("\n");
}

// 공백 정규화 — 단어 사이 공백은 보존, 줄끝 공백/과다 빈줄/선·후행 빈줄만 정리.
export function normalizeWhitespace(s) {
  return String(s)
    .replace(/\r\n?/g, "\n") // CRLF/CR → LF
    .replace(/[ \t]+$/gm, "") // 줄 끝 공백 제거 (단어 사이 공백은 건드리지 않음)
    .replace(/\n{3,}/g, "\n\n") // 3+ 연속 빈줄 → 2
    .replace(/^\n+/, "") // 선두 빈줄 제거
    .replace(/\n+$/, ""); // 말미 빈줄 제거
}

// 표시 직전 본문 정제: ANSI → chrome → 공백 정규화 순서.
export function cleanMessageText(raw) {
  if (raw == null) return "";
  let t = stripAnsi(raw);
  t = stripTerminalChrome(t);
  t = normalizeWhitespace(t);
  return t;
}

// 말풍선 본문 최종 렌더: 정제 후 마크다운(표·헤더·코드블록·인용·리스트·이모지) 렌더.
// renderMarkdown 이 입력 전체를 HTML escape 하므로 raw HTML 은 실행되지 않는다(XSS 안전).
export function renderMessageBody(raw) {
  return renderMarkdown(cleanMessageText(raw));
}

export default renderMessageBody;
