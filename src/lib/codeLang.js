// CodeMirror 6 언어 해석 + 언어팩 동적 로드 (코드 뷰어/에디터용).
//
// 책임:
//   1) BE language_hint(우선) 또는 파일 확장자(fallback) → 정규 언어 id 로 매핑(resolveLangId).
//   2) 정규 id → CM6 언어 Extension 을 dynamic import 로 로드(loadLanguageExtension).
//      → 언어팩은 코드 파일을 처음 열 때, 해당 언어만 비동기 로드되어 초기 번들에서 분리된다.
//   3) 사람이 읽는 라벨(languageLabel) — 헤더 배지 표시용.
//
// 알 수 없는 언어는 null 반환 → 호출부가 하이라이팅 없이 일반 텍스트로 렌더(안전 폴백).

import { StreamLanguage } from "@codemirror/language";

// 정규 id → 언어 Extension 로더(동적 import). 각 로더는 1회 import 후 번들 청크 캐시됨.
//   - 공식 lang-* 패키지: LanguageSupport 반환
//   - legacy-modes: StreamLanguage.define(...) 로 래핑
const LOADERS = {
  javascript: () => import("@codemirror/lang-javascript").then((m) => m.javascript()),
  jsx: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ jsx: true })),
  typescript: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ typescript: true })),
  tsx: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ typescript: true, jsx: true })),
  python: () => import("@codemirror/lang-python").then((m) => m.python()),
  json: () => import("@codemirror/lang-json").then((m) => m.json()),
  yaml: () => import("@codemirror/lang-yaml").then((m) => m.yaml()),
  html: () => import("@codemirror/lang-html").then((m) => m.html()),
  css: () => import("@codemirror/lang-css").then((m) => m.css()),
  markdown: () => import("@codemirror/lang-markdown").then((m) => m.markdown()),
  rust: () => import("@codemirror/lang-rust").then((m) => m.rust()),
  sql: () => import("@codemirror/lang-sql").then((m) => m.sql()),
  xml: () => import("@codemirror/lang-xml").then((m) => m.xml()),
  cpp: () => import("@codemirror/lang-cpp").then((m) => m.cpp()),
  java: () => import("@codemirror/lang-java").then((m) => m.java()),
  php: () => import("@codemirror/lang-php").then((m) => m.php()),
  go: () => import("@codemirror/lang-go").then((m) => m.go()),
  vue: () => import("@codemirror/lang-vue").then((m) => m.vue()),
  // legacy stream modes (공식 lang 패키지 없는 언어)
  shell: () => import("@codemirror/legacy-modes/mode/shell").then((m) => StreamLanguage.define(m.shell)),
  toml: () => import("@codemirror/legacy-modes/mode/toml").then((m) => StreamLanguage.define(m.toml)),
  dockerfile: () => import("@codemirror/legacy-modes/mode/dockerfile").then((m) => StreamLanguage.define(m.dockerFile)),
  ruby: () => import("@codemirror/legacy-modes/mode/ruby").then((m) => StreamLanguage.define(m.ruby)),
  lua: () => import("@codemirror/legacy-modes/mode/lua").then((m) => StreamLanguage.define(m.lua)),
  perl: () => import("@codemirror/legacy-modes/mode/perl").then((m) => StreamLanguage.define(m.perl)),
  properties: () => import("@codemirror/legacy-modes/mode/properties").then((m) => StreamLanguage.define(m.properties)),
};

// 사람이 읽는 라벨(헤더 배지). 정규 id → 표시명.
const LABELS = {
  javascript: "JavaScript", jsx: "JSX", typescript: "TypeScript", tsx: "TSX",
  python: "Python", json: "JSON", yaml: "YAML", html: "HTML", css: "CSS",
  markdown: "Markdown", rust: "Rust", sql: "SQL", xml: "XML", cpp: "C/C++",
  java: "Java", php: "PHP", go: "Go", vue: "Vue", shell: "Shell", toml: "TOML",
  dockerfile: "Dockerfile", ruby: "Ruby", lua: "Lua", perl: "Perl", properties: "INI",
};

// language_hint / 확장자 별칭 → 정규 id. (소문자, 점 제거 후 조회)
const ALIAS = {
  js: "javascript", mjs: "javascript", cjs: "javascript", javascript: "javascript", node: "javascript",
  jsx: "jsx",
  ts: "typescript", typescript: "typescript", mts: "typescript", cts: "typescript",
  tsx: "tsx",
  py: "python", python: "python", pyi: "python", pyw: "python",
  json: "json", json5: "json", jsonc: "json", geojson: "json",
  yaml: "yaml", yml: "yaml",
  html: "html", htm: "html", xhtml: "html",
  css: "css", scss: "css", sass: "css", less: "css",
  md: "markdown", markdown: "markdown", mdx: "markdown", mkd: "markdown",
  rust: "rust", rs: "rust",
  sql: "sql",
  xml: "xml", svg: "xml", xsl: "xml", plist: "xml", rss: "xml",
  c: "cpp", h: "cpp", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hxx: "cpp", "c++": "cpp",
  java: "java",
  php: "php", php5: "php", phtml: "php",
  go: "go", golang: "go",
  vue: "vue",
  sh: "shell", bash: "shell", zsh: "shell", ksh: "shell", shell: "shell", bashrc: "shell",
  toml: "toml",
  dockerfile: "dockerfile", containerfile: "dockerfile",
  rb: "ruby", ruby: "ruby", gemfile: "ruby", rake: "ruby",
  lua: "lua",
  pl: "perl", pm: "perl", perl: "perl",
  ini: "properties", properties: "properties", conf: "properties", cfg: "properties", env: "properties", editorconfig: "properties",
};

function norm(s) {
  if (s == null) return "";
  return String(s).trim().toLowerCase().replace(/^\./, "");
}

// language_hint(우선) → 확장자(fallback) → 정규 id. 매핑 실패 시 null(일반 텍스트).
export function resolveLangId(languageHint, extension) {
  const h = norm(languageHint);
  if (h && ALIAS[h]) return ALIAS[h];
  const e = norm(extension);
  if (e && ALIAS[e]) return ALIAS[e];
  // hint 가 이미 정규 id 인 경우(별칭 테이블엔 없지만 로더엔 있는) 직접 매칭
  if (h && LOADERS[h]) return h;
  if (e && LOADERS[e]) return e;
  return null;
}

// 정규 id → CM6 언어 Extension(동적 로드). 알 수 없으면 null.
export async function loadLanguageExtension(langId) {
  const loader = LOADERS[langId];
  if (!loader) return null;
  try {
    return await loader();
  } catch {
    return null; // 언어팩 로드 실패 → 하이라이팅 없이 텍스트(깨짐 없음)
  }
}

// 정규 id → 표시 라벨. 없으면 대문자 폴백.
export function languageLabel(langId) {
  if (!langId) return "TEXT";
  return LABELS[langId] || langId.toUpperCase();
}
