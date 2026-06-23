// 파일트리 전용 Material Icon Theme 매핑 (S-04 ArtifactTree).
//
// 출처: material-icon-theme — https://github.com/material-extensions/vscode-material-icon-theme
// 라이선스: MIT (src/assets/file-icons/LICENSE, 루트 NOTICE 참조).
//
// 방식: node_modules 런타임 import 의존을 없애기 위해, 트리에서 실제 쓰는 타입의 SVG'만'
//   src/assets/file-icons/ 로 '복사'해 두고 Vite `?raw` 로 정적 import 한다(프로젝트 내부 파일).
//   → dev/빌드/배포 모두 안정적이고, 롤백은 이 파일 + assets/file-icons/ 삭제로 끝난다.
// 매핑 없는 확장자/폴더는 기본 document/folder 아이콘으로 fallback.

// ── 파일 아이콘 SVG (raw) ────────────────────────────────────
import document from "../assets/file-icons/document.svg?raw";
import markdown from "../assets/file-icons/markdown.svg?raw";
import html from "../assets/file-icons/html.svg?raw";
import json from "../assets/file-icons/json.svg?raw";
import rust from "../assets/file-icons/rust.svg?raw";
import javascript from "../assets/file-icons/javascript.svg?raw";
import typescript from "../assets/file-icons/typescript.svg?raw";
import vue from "../assets/file-icons/vue.svg?raw";
import react from "../assets/file-icons/react.svg?raw";
import database from "../assets/file-icons/database.svg?raw";
import image from "../assets/file-icons/image.svg?raw";
import svgIcon from "../assets/file-icons/svg.svg?raw";
import pdf from "../assets/file-icons/pdf.svg?raw";
import console_ from "../assets/file-icons/console.svg?raw";
import yaml from "../assets/file-icons/yaml.svg?raw";
import toml from "../assets/file-icons/toml.svg?raw";
import xml from "../assets/file-icons/xml.svg?raw";
import css from "../assets/file-icons/css.svg?raw";
import python from "../assets/file-icons/python.svg?raw";
import docker from "../assets/file-icons/docker.svg?raw";
import lock from "../assets/file-icons/lock.svg?raw";
import git from "../assets/file-icons/git.svg?raw";
import tsconfig from "../assets/file-icons/tsconfig.svg?raw";

// ── 폴더 아이콘 SVG (닫힘/열림 쌍) ───────────────────────────
import folder from "../assets/file-icons/folder.svg?raw";
import folderOpen from "../assets/file-icons/folder-open.svg?raw";
import folderSrc from "../assets/file-icons/folder-src.svg?raw";
import folderSrcOpen from "../assets/file-icons/folder-src-open.svg?raw";
import folderDist from "../assets/file-icons/folder-dist.svg?raw";
import folderDistOpen from "../assets/file-icons/folder-dist-open.svg?raw";
import folderDocs from "../assets/file-icons/folder-docs.svg?raw";
import folderDocsOpen from "../assets/file-icons/folder-docs-open.svg?raw";
import folderTest from "../assets/file-icons/folder-test.svg?raw";
import folderTestOpen from "../assets/file-icons/folder-test-open.svg?raw";
import folderComponents from "../assets/file-icons/folder-components.svg?raw";
import folderComponentsOpen from "../assets/file-icons/folder-components-open.svg?raw";
import folderImages from "../assets/file-icons/folder-images.svg?raw";
import folderImagesOpen from "../assets/file-icons/folder-images-open.svg?raw";
import folderConfig from "../assets/file-icons/folder-config.svg?raw";
import folderConfigOpen from "../assets/file-icons/folder-config-open.svg?raw";
import folderScripts from "../assets/file-icons/folder-scripts.svg?raw";
import folderScriptsOpen from "../assets/file-icons/folder-scripts-open.svg?raw";
import folderNode from "../assets/file-icons/folder-node.svg?raw";
import folderNodeOpen from "../assets/file-icons/folder-node-open.svg?raw";
import folderPublic from "../assets/file-icons/folder-public.svg?raw";
import folderPublicOpen from "../assets/file-icons/folder-public-open.svg?raw";

const FILE_ICONS = {
  document, markdown, html, json, rust, javascript, typescript, vue, react,
  database, image, svg: svgIcon, pdf, console: console_, yaml, toml, xml,
  css, python, docker, lock, git, tsconfig,
};

// 확장자(소문자, 점 제외) → 아이콘 키. 미등록은 document fallback.
const EXT_MAP = {
  md: "markdown", markdown: "markdown",
  html: "html", htm: "html",
  json: "json", jsonc: "json",
  rs: "rust",
  js: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript",
  vue: "vue",
  jsx: "react", tsx: "react",
  sql: "database",
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image", bmp: "image", ico: "image",
  svg: "svg",
  pdf: "pdf",
  sh: "console", bash: "console", zsh: "console",
  yaml: "yaml", yml: "yaml",
  toml: "toml",
  xml: "xml",
  css: "css",
  py: "python",
  lock: "lock",
  txt: "document",
};

// 확장자 없이 파일명으로 판정하는 특수 케이스(소문자 풀네임).
const FILENAME_MAP = {
  dockerfile: "docker",
  ".gitignore": "git",
  ".gitattributes": "git",
  "tsconfig.json": "tsconfig",
};

// 폴더명(소문자) → {closed, open} 쌍. 미등록은 기본 folder/folder-open.
const FOLDER_ICONS = {
  src: [folderSrc, folderSrcOpen],
  dist: [folderDist, folderDistOpen],
  build: [folderDist, folderDistOpen],
  docs: [folderDocs, folderDocsOpen],
  documents: [folderDocs, folderDocsOpen],
  test: [folderTest, folderTestOpen],
  tests: [folderTest, folderTestOpen],
  __tests__: [folderTest, folderTestOpen],
  components: [folderComponents, folderComponentsOpen],
  images: [folderImages, folderImagesOpen],
  img: [folderImages, folderImagesOpen],
  assets: [folderImages, folderImagesOpen],
  config: [folderConfig, folderConfigOpen],
  scripts: [folderScripts, folderScriptsOpen],
  node_modules: [folderNode, folderNodeOpen],
  public: [folderPublic, folderPublicOpen],
};

// svg 문자열이 width/height 를 명시하지 않으므로(viewBox 만 있음) 부모 박스를 꽉 채우도록 주입한다.
function fit(svg) {
  return svg.replace(/<svg /, '<svg width="100%" height="100%" ');
}

export function fileIconSvg(name, ext) {
  const lower = (name || "").toLowerCase();
  if (FILENAME_MAP[lower]) return fit(FILE_ICONS[FILENAME_MAP[lower]]);
  const e = (ext || "").toLowerCase().replace(/^\./, "");
  const key = EXT_MAP[e];
  return fit(FILE_ICONS[key] || FILE_ICONS.document);
}

export function folderIconSvg(name, open) {
  const lower = (name || "").toLowerCase();
  const pair = FOLDER_ICONS[lower];
  if (pair) return fit(open ? pair[1] : pair[0]);
  return fit(open ? folderOpen : folder);
}
