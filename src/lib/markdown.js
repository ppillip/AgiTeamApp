// 의존성 없는 경량 Markdown → HTML 렌더러 (DV-40.4 산출물 뷰어용).
//
// 보안 원칙(가이드 §5 / DS-40 §17.6 XSS): **입력 전체를 먼저 HTML escape** 한 뒤,
// escape 된 안전한 텍스트 위에서만 우리가 직접 태그를 생성한다. 따라서 원문에 포함된
// raw HTML 은 그대로 텍스트로 표시되며 실행되지 않는다. (marked+DOMPurify 미사용, 무의존)
//
// 지원: 제목(#~######), 수평선, 코드펜스(```), 인용(>), 순서/비순서 목록(중첩 1단계),
//       GFM 파이프 테이블, 단락, 인라인(코드 `x`, **굵게**, *기울임*, [링크](url)).

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// 인라인 변환. 입력은 "이미 escape 된" 텍스트.
function inline(text) {
  let t = text;
  // 인라인 코드: `code` (내부는 추가 변환 안 함)
  t = t.replace(/`([^`]+)`/g, (_, c) => `<code class="md-code">${c}</code>`);
  // 굵게 **x** / __x__
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  // 기울임 *x* / _x_
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  t = t.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  // 링크 [text](url) — url 은 http(s)/상대/앵커만 허용 (javascript: 등 차단)
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
    const safe = /^(https?:\/\/|\/|#|\.\/|\.\.\/)/.test(url) ? url : "#";
    const ext = /^https?:/.test(safe);
    return `<a href="${safe}" class="md-link"${ext ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`;
  });
  return t;
}

function renderTable(rows) {
  // rows: escape 된 라인 문자열 배열 (헤더, 구분선, 본문...)
  const splitCells = (line) =>
    line
      .replace(/^\s*\|/, "")
      .replace(/\|\s*$/, "")
      .split("|")
      .map((c) => c.trim());
  const header = splitCells(rows[0]);
  const body = rows.slice(2).map(splitCells);
  let html = '<table class="md-table"><thead><tr>';
  html += header.map((h) => `<th>${inline(h)}</th>`).join("");
  html += "</tr></thead><tbody>";
  for (const r of body) {
    html += "<tr>" + header.map((_, i) => `<td>${inline(r[i] ?? "")}</td>`).join("") + "</tr>";
  }
  html += "</tbody></table>";
  return html;
}

export function renderMarkdown(src) {
  if (src == null) return "";
  const lines = escapeHtml(src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  const isTableSep = (s) => /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(s);

  while (i < lines.length) {
    const line = lines[i];

    // 코드펜스
    const fence = line.match(/^\s*```(.*)$/);
    if (fence) {
      const lang = fence[1].trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // 닫는 펜스 소비
      out.push(
        `<pre class="md-pre"><code${lang ? ` data-lang="${lang}"` : ""}>${buf.join("\n")}</code></pre>`
      );
      continue;
    }

    // 빈 줄
    if (/^\s*$/.test(line)) {
      i++;
      continue;
    }

    // 수평선
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      out.push('<hr class="md-hr" />');
      i++;
      continue;
    }

    // 제목
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = h[1].length;
      out.push(`<h${lvl} class="md-h md-h${lvl}">${inline(h[2].trim())}</h${lvl}>`);
      i++;
      continue;
    }

    // 테이블 (현재 줄이 헤더, 다음 줄이 구분선)
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const tbl = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i])) {
        tbl.push(lines[i]);
        i++;
      }
      out.push(renderTable(tbl));
      continue;
    }

    // 인용
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote class="md-quote">${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    // 목록 (순서/비순서)
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const tag = ordered ? "ol" : "ul";
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        const content = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "");
        items.push(`<li>${inline(content)}</li>`);
        i++;
      }
      out.push(`<${tag} class="md-list">${items.join("")}</${tag}>`);
      continue;
    }

    // 단락 (연속 비빈 줄을 묶음, <br/> 로 연결)
    const para = [];
    while (
      i < lines.length &&
      !/^\s*$/.test(lines[i]) &&
      !/^\s*(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*```/.test(lines[i]) &&
      !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))
    ) {
      para.push(lines[i]);
      i++;
    }
    out.push(`<p class="md-p">${para.map(inline).join("<br/>")}</p>`);
  }

  return out.join("\n");
}

export default renderMarkdown;
