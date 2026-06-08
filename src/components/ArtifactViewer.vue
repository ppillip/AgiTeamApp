<script>
import Icon from "./Icon.vue";
import { store, closeViewer } from "../stores/monitor.js";
import { fileStreamUrl } from "../api/index.js";
import { renderMarkdown } from "../lib/markdown.js";

// mermaid 동적 로드(초기 번들 분리 — md 뷰어에서 처음 다이어그램을 만날 때만 로드).
// securityLevel:'strict' 로 mermaid 내부 XSS/스크립트 차단.
let _mermaidPromise = null;
function loadMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = import("mermaid").then((m) => {
      const mermaid = m.default || m;
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default" });
      return mermaid;
    });
  }
  return _mermaidPromise;
}
let _mermaidSeq = 0; // SVG id 충돌 방지용 단조 증가 시드

// 산출물 뷰어 (S-05) — render_mode 별 분기(불칸 계약):
//   markdown      → 경량 MD 렌더(무의존, XSS 안전)
//   pdf_stream    → stream_url iframe 임베드
//   html          → 샌드박스 iframe 렌더(+ 렌더/소스 토글)  [백엔드: render_mode='html', content=raw html 또는 stream_url]
//   image         → <img> 렌더(svg/png/jpg)                [백엔드: render_mode='image', stream_url(+ content=svg 텍스트 옵션)]
//   converted_preview / unsupported → 안내 + 다운로드
//
// big=true: '크게' 인라인 확대 모드(채팅 영역 자리에 큰 뷰). 우측 트리에서 파일을 클릭하면
// store.viewer 공유로 가운데 큰 뷰 내용이 즉시 교체된다(닫지 않고 연속 열람). 복귀 시 채팅 원복.
export default {
  name: "ArtifactViewer",
  components: { Icon },
  props: { big: { type: Boolean, default: false } },
  emits: ["expand", "collapse"],
  data() {
    return { htmlMode: "render" }; // render | source
  },
  computed: {
    store: () => store,
    v() {
      return store.viewer;
    },
    file() {
      return store.viewer.file;
    },
    mode() {
      // 백엔드 render_mode 정규화(별칭 흡수): html_iframe→html, svg_inline/image_inline/svg→image
      const m = this.file?.renderMode || null;
      if (m === "html_iframe") return "html";
      if (m === "image" || m === "image_inline" || m === "svg_inline" || m === "svg") return "image";
      return m;
    },
    mdHtml() {
      return this.mode === "markdown" && this.file?.content != null ? renderMarkdown(this.file.content) : "";
    },
    // pdf/html/image 공용 스트림 URL. 백엔드 stream_url 은 project_id 가 빠져 있어
    // 선택 프로젝트 기준으로 항상 직접 구성(project_id 포함)한다.
    streamUrl() {
      if (!this.file) return null;
      return fileStreamUrl(this.file.path, "original", store.selectedProjectId);
    },
    // html content(raw)를 주면 srcdoc 으로 안전 렌더(추가 요청 없음). 없으면 stream 으로.
    htmlSrcdoc() {
      return this.mode === "html" ? this.file?.content ?? null : null;
    },
    isImage() {
      return this.mode === "image";
    },
    extBadge() {
      return (this.file?.ext || "").toUpperCase();
    },
    downloadHref() {
      return this.streamUrl;
    },
  },
  watch: {
    // 파일이 바뀌면 html 토글을 렌더로 초기화
    "store.viewer.path"() {
      this.htmlMode = "render";
    },
    // md 렌더 결과가 바뀌면 mermaid 다이어그램 변환(파일 전환·내용 갱신 포함)
    mdHtml() {
      this.renderMermaid();
    },
  },
  mounted() {
    this.renderMermaid();
  },
  methods: {
    closeViewer,
    // md 본문 내 .mermaid-block 들을 SVG 다이어그램으로 변환. 실패한 블록은 코드블록으로 폴백.
    async renderMermaid() {
      await this.$nextTick();
      const root = this.$refs.mdBody;
      if (!root) return;
      const nodes = Array.from(root.querySelectorAll(".mermaid-block:not([data-done])"));
      if (!nodes.length) return;
      let mermaid;
      try {
        mermaid = await loadMermaid();
      } catch {
        return; // mermaid 로드 실패 → 블록은 텍스트 그대로 둠(깨짐 없음)
      }
      if (this.$refs.mdBody !== root) return; // 그 사이 파일 전환되면 폐기
      for (const node of nodes) {
        const src = node.getAttribute("data-src") || node.textContent || "";
        const id = "mmd-" + ++_mermaidSeq;
        try {
          const { svg } = await mermaid.render(id, src);
          node.innerHTML = svg;
          node.setAttribute("data-done", "1");
        } catch {
          // 파싱 실패 → 원본을 코드블록으로 폴백(머메이드 소스라도 읽히게)
          const pre = document.createElement("pre");
          pre.className = "md-pre";
          const codeEl = document.createElement("code");
          codeEl.setAttribute("data-lang", "mermaid");
          codeEl.textContent = src;
          pre.appendChild(codeEl);
          node.replaceWith(pre);
        }
      }
    },
  },
};
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col" :class="big ? 'overflow-hidden rounded-2xl border border-line bg-white' : 'p-4'">
    <!-- 헤더 -->
    <div class="flex items-center justify-between gap-2" :class="big ? 'border-b border-line-soft px-5 py-3' : 'mb-3'">
      <div class="flex min-w-0 items-center gap-[9px] font-semibold" :class="big ? 'text-[14px]' : 'text-[13.5px]'">
        <span v-if="file" class="flex-shrink-0 rounded-md bg-amber-tint px-1.5 py-[3px] text-[10px] font-extrabold text-amber-600">{{ extBadge || "DOC" }}</span>
        <span class="truncate">{{ file ? file.name : "산출물 뷰어" }}</span>
        <span v-if="big" class="hidden flex-shrink-0 text-[11.5px] font-medium text-ink-400 sm:inline">· 트리에서 파일을 클릭하면 여기로 열립니다</span>
      </div>
      <div class="flex flex-shrink-0 items-center gap-1.5">
        <!-- html 렌더/소스 토글 -->
        <div v-if="file && mode === 'html'" class="inline-flex items-center gap-0.5 rounded-lg border border-line bg-[#F7F7F8] p-0.5">
          <button @click="htmlMode = 'render'" class="rounded-md px-2.5 py-1 text-[11.5px] font-bold" :class="htmlMode === 'render' ? 'bg-amber text-white' : 'text-ink-600'">렌더</button>
          <button @click="htmlMode = 'source'" class="rounded-md px-2.5 py-1 text-[11.5px] font-bold" :class="htmlMode === 'source' ? 'bg-amber text-white' : 'text-ink-600'">소스</button>
        </div>
        <!-- 큰 뷰: 채팅으로 복귀 / 패널: 크게 + 닫기 -->
        <button v-if="big" @click="$emit('collapse')" class="flex items-center gap-1.5 rounded-lg bg-[#F4F4F6] px-3 py-1.5 text-[12.5px] font-semibold text-ink-600 hover:bg-line-soft" title="채팅으로 돌아가기">
          <Icon name="x" :size="15" />채팅으로
        </button>
        <template v-else>
          <button v-if="file" @click="$emit('expand')" class="flex items-center gap-1.5 rounded-lg bg-[#F4F4F6] px-[13px] py-1.5 text-[12.5px] font-semibold text-ink-600 hover:bg-line-soft" title="채팅 영역에 크게 보기">
            <Icon name="expand" :size="14" />크게
          </button>
          <button v-if="v.open" @click="closeViewer" class="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-ink-500 hover:bg-[#F4F4F6]"><Icon name="x" :size="16" /></button>
        </template>
      </div>
    </div>

    <!-- 본문 -->
    <div class="relative min-h-0 flex-1 overflow-hidden bg-white" :class="big ? '' : 'rounded-[13px] border border-line'">
      <!-- 안내(파일 미선택) -->
      <div v-if="!v.open" class="flex h-full items-center justify-center px-6 text-center text-[13px] text-ink-400">
        {{ big ? "우측 트리에서 파일을 클릭하면 여기에 크게 표시됩니다." : "좌측 트리에서 파일을 클릭하면" }}<br v-if="!big" />여기에 내용이 표시됩니다.
      </div>
      <!-- 로딩 -->
      <div v-else-if="v.loading" class="flex h-full items-center justify-center text-[13px] text-ink-400">불러오는 중…</div>
      <!-- 에러 -->
      <div v-else-if="v.error" class="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <Icon name="alert" :size="22" class="text-red-400" />
        <div class="text-[13px] font-semibold text-red-500">{{ v.error }}</div>
        <a v-if="downloadHref" :href="downloadHref" target="_blank" rel="noopener noreferrer" class="mt-1 inline-flex items-center gap-1.5 rounded-[10px] border border-line px-3.5 py-2 text-[13px] font-semibold text-ink-600 hover:bg-[#F4F4F6]">
          <Icon name="download" :size="15" />원본 열기
        </a>
      </div>

      <template v-else-if="file">
        <!-- markdown -->
        <div
          v-if="mode === 'markdown'"
          ref="mdBody"
          class="md-body h-full overflow-y-auto nice-scroll"
          :class="big ? 'px-10 py-7 lg:px-16' : 'px-[22px] py-5'"
          v-html="mdHtml"
        ></div>

        <!-- pdf -->
        <iframe v-else-if="mode === 'pdf_stream' && streamUrl" :src="streamUrl" class="h-full w-full border-0" title="PDF 미리보기"></iframe>

        <!-- html: 샌드박스 iframe(스크립트 차단). content 있으면 srcdoc, 없으면 stream. 소스 모드는 raw 표시 -->
        <template v-else-if="mode === 'html'">
          <iframe
            v-if="htmlMode === 'render'"
            :srcdoc="htmlSrcdoc || undefined"
            :src="htmlSrcdoc ? undefined : streamUrl"
            sandbox="allow-same-origin"
            referrerpolicy="no-referrer"
            class="h-full w-full border-0 bg-white"
            title="HTML 렌더(샌드박스)"
          ></iframe>
          <div v-else class="md-body h-full overflow-auto px-5 py-4 nice-scroll">
            <pre class="md-pre whitespace-pre-wrap break-words">{{ file.content || "(소스를 불러올 수 없습니다 — 백엔드 raw 미지원)" }}</pre>
          </div>
        </template>

        <!-- image: svg/png/jpg -->
        <div v-else-if="mode === 'image'" class="flex h-full items-center justify-center overflow-auto bg-[#FAFAFB] p-4 nice-scroll">
          <img :src="streamUrl" :alt="file.name" class="max-h-full max-w-full object-contain" />
        </div>

        <!-- pptx / docx 변환 대기 또는 미지원 -->
        <div v-else class="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
          <Icon name="fileText" :size="30" class="text-ink-300" />
          <div class="text-[14px] font-semibold text-ink-700">{{ file.name }}</div>
          <div class="text-[13px] text-ink-400" v-if="mode === 'converted_preview'">
            {{ file.ext ? file.ext.toUpperCase() : "문서" }} 미리보기 변환을 준비 중입니다.<br />변환이 완료되면 이곳에 렌더됩니다.
          </div>
          <div class="text-[13px] text-ink-400" v-else>이 형식은 미리보기를 지원하지 않습니다.</div>
          <a v-if="downloadHref" :href="downloadHref" target="_blank" rel="noopener noreferrer" class="mt-1 inline-flex items-center gap-1.5 rounded-[10px] border border-line px-3.5 py-2 text-[13px] font-semibold text-ink-600 hover:bg-[#F4F4F6]">
            <Icon name="download" :size="15" />원본 열기
          </a>
        </div>
      </template>
    </div>
  </div>
</template>
