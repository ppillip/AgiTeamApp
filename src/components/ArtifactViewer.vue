<script>
import { defineAsyncComponent } from "vue";
import Icon from "./Icon.vue";
import { store, closeViewer, saveArtifact } from "../stores/monitor.js";
import { fileStreamUrl } from "../api/index.js";
import { renderMarkdown } from "../lib/markdown.js";

// CodeMirror 코드 에디터는 코드 파일을 처음 열 때만 로드(초기 번들 분리, 아테나 설계).
const CodeMirrorEditor = defineAsyncComponent(() => import("./CodeMirrorEditor.vue"));

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

// Milkdown Crepe(WYSIWYG 마크다운 에디터) 동적 로드 — [수정] 탭을 처음 열 때만 로드.
// mermaid 와 같은 패턴으로 초기 번들에서 분리한다(에디터·테마 CSS 포함, ~수백 kB).
// 옵시디언처럼 '렌더된 채로 그 자리 편집' + 마크다운 1급 입출력(getMarkdown).
let _crepePromise = null;
function loadCrepe() {
  if (!_crepePromise) {
    _crepePromise = Promise.all([
      import("@milkdown/crepe"),
      import("@milkdown/crepe/theme/common/style.css"),
      import("@milkdown/crepe/theme/frame.css"),
    ]).then(([m]) => m.Crepe);
  }
  return _crepePromise;
}

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
  components: { Icon, CodeMirrorEditor },
  props: { big: { type: Boolean, default: false } },
  emits: ["expand", "collapse"],
  data() {
    return {
      htmlMode: "render", // html: render | source
      // 마크다운 에디터(옵시디언식 WYSIWYG): render(렌더) | edit(수정)
      mdTab: "render",
      saving: false,
      saveError: null,
      editorLoading: false, // Crepe 동적 로드/마운트 중
      dirtyFlag: false, // 에디터 내용이 마운트 시점 대비 변경됨(저장 버튼 활성 조건)
      // 코드 에디터(CodeMirror): 단일 편집화면(렌더/수정 탭 없음). 현재 편집 내용 + dirty.
      codeContent: "",
      codeDirty: false,
    };
    // 비반응 인스턴스 필드(아래 created 에서 초기화): this.crepe, this.editorBase
  },
  created() {
    this.crepe = null; // Crepe 인스턴스(반응형 래핑 회피 — ProseMirror 보호)
    this.editorBase = ""; // 마운트 직후 정규화된 마크다운(dirty 비교 기준)
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
    isMarkdown() {
      return this.mode === "markdown";
    },
    isCode() {
      return this.mode === "code";
    },
    // 코드 에디터 props
    codeLanguageHint() {
      return this.file?.languageHint ?? null;
    },
    codeExtension() {
      return this.file?.ext ?? null;
    },
    // 오프라인(degraded)에선 저장 불가 → 읽기 전용으로 표시(혼란 방지)
    codeReadonly() {
      return store.degraded;
    },
    codeCanSave() {
      return this.isCode && this.codeDirty && !this.saving && !store.degraded;
    },
    // 수정 기준 원문(현재 저장본). edit 진입 시 에디터 초기값.
    mdBase() {
      return this.file?.content ?? "";
    },
    // 변경 여부(저장 버튼 활성 조건). WYSIWYG 에디터의 markdownUpdated 가 dirtyFlag 를 갱신.
    dirty() {
      return this.mdTab === "edit" && this.dirtyFlag;
    },
    canSave() {
      return this.dirty && !this.saving && !this.editorLoading && !store.degraded;
    },
    // pdf/html/image 공용 스트림 URL. 백엔드 stream_url 은 project_id 가 빠져 있어
    // 선택 프로젝트 기준으로 항상 직접 구성(project_id 포함)한다.
    streamUrl() {
      if (!this.file) return null;
      return fileStreamUrl(this.file.path, "original", store.selectedProjectId, store.rootType);
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
    // 파일이 바뀌면 html 토글·md 에디터 상태를 렌더로 초기화(열려 있던 에디터는 언마운트)
    "store.viewer.path"() {
      this.htmlMode = "render";
      this.unmountEditor(); // 이전 파일 에디터 폐기(있으면)
      this.mdTab = "render";
      this.saveError = null;
      // 코드 에디터 상태 초기화(새 파일 내용 기준)
      this.codeContent = this.file?.content ?? "";
      this.codeDirty = false;
    },
    // 파일 내용이 외부 갱신(저장/외부수정 reload)되면 코드 baseline 동기화
    "store.viewer.file.content"() {
      if (this.isCode && !this.codeDirty) this.codeContent = this.file?.content ?? "";
    },
    // md 렌더 결과가 바뀌면 mermaid 다이어그램 변환(파일 전환·내용 갱신 포함)
    mdHtml() {
      this.renderMermaid();
    },
  },
  mounted() {
    this.renderMermaid();
    // Ctrl/Cmd+S 저장 단축키(수정 모드일 때만 가로채 저장)
    window.addEventListener("keydown", this.onKeydown);
  },
  beforeUnmount() {
    window.removeEventListener("keydown", this.onKeydown);
    this.unmountEditor();
  },
  methods: {
    closeViewer,
    // 렌더↔수정 탭 전환. 수정 진입 시 WYSIWYG 에디터 로드, 렌더 복귀 시 언마운트(요구사항).
    setMdTab(tab) {
      if (tab === this.mdTab) return;
      this.saveError = null;
      if (tab === "edit") {
        this.mdTab = "edit";
        this.$nextTick(() => this.mountEditor());
      } else {
        this.unmountEditor(); // [렌더] 전환 → 에디터 언마운트
        this.mdTab = "render";
      }
    },
    // Crepe(WYSIWYG) 마운트: 현재 저장본을 렌더된 상태로 띄우고, 그 자리에서 편집 가능.
    //   markdownUpdated 리스너로 dirty 추적(정규화된 초기값 editorBase 와 비교 → 오탐 방지).
    async mountEditor() {
      const host = this.$refs.mdEditor;
      if (!host || this.crepe) return;
      this.editorLoading = true;
      this.dirtyFlag = false;
      const path = this.file?.path;
      try {
        const Crepe = await loadCrepe();
        // 로드 중 파일 전환·탭 이탈 시 폐기(레이스 방어)
        if (this.mdTab !== "edit" || this.file?.path !== path || !this.$refs.mdEditor) {
          this.editorLoading = false;
          return;
        }
        const crepe = new Crepe({ root: this.$refs.mdEditor, defaultValue: this.mdBase });
        crepe.on((listener) => {
          listener.markdownUpdated((_ctx, markdown) => {
            // 정규화된 초기값과 다를 때만 dirty(왕복 정규화로 인한 오탐 차단)
            this.dirtyFlag = markdown !== this.editorBase;
          });
        });
        await crepe.create();
        // create 중 파일 전환·탭 이탈했으면 즉시 폐기(스테일 인스턴스 누수 방지)
        if (this.mdTab !== "edit" || this.file?.path !== path) {
          try { crepe.destroy(); } catch {}
          this.editorLoading = false;
          return;
        }
        // 마운트 직후 정규화된 마크다운을 dirty 비교 기준으로 고정
        this.editorBase = crepe.getMarkdown();
        this.crepe = crepe;
      } catch (e) {
        this.saveError = "에디터를 불러오지 못했습니다.";
        this.mdTab = "render";
      } finally {
        this.editorLoading = false;
      }
    },
    // Crepe 언마운트(에디터 정리). 비동기 destroy 는 호스트 DOM(v-show)이 유지되므로 안전.
    unmountEditor() {
      const inst = this.crepe;
      this.crepe = null;
      this.dirtyFlag = false;
      this.editorBase = "";
      if (inst) {
        try {
          inst.destroy();
        } catch {}
      }
    },
    // 저장: 에디터의 현재 마크다운을 백엔드에 기록(store.saveArtifact) → 성공 시 렌더 탭 자동 전환.
    async save() {
      if (!this.file || this.saving) return;
      const md = this.crepe ? this.crepe.getMarkdown() : null;
      if (md == null) return;
      this.saving = true;
      this.saveError = null;
      try {
        await saveArtifact(this.file.path, md);
        this.unmountEditor(); // 저장 후 에디터 정리
        this.mdTab = "render"; // 저장 성공 → 렌더 탭 자동 전환(요구사항)
      } catch (e) {
        this.saveError = e?.message || "저장에 실패했습니다.";
      } finally {
        this.saving = false;
      }
    },
    // Ctrl+S / Cmd+S — 마크다운 수정 모드 또는 코드 모드에서 가로채 저장(그 외엔 브라우저 기본 동작 유지).
    // (코드 모드는 CodeMirror 내부 keymap 도 save 를 emit 하지만, 에디터 밖 포커스 시를 위해 window 도 커버)
    onKeydown(e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "S")) {
        if (this.isMarkdown && this.mdTab === "edit") {
          e.preventDefault();
          if (this.canSave) this.save();
        } else if (this.isCode) {
          e.preventDefault();
          if (this.codeCanSave) this.saveCode();
        }
      }
    },
    // 코드 에디터 편집 → dirty 갱신(현재 저장본과 다를 때만)
    onCodeInput(val) {
      this.codeContent = val;
      this.codeDirty = val !== (this.file?.content ?? "");
    },
    // 코드 저장: 기존 saveArtifact/writeFile 재사용(rootType 전달 유지). 성공 시 baseline 갱신.
    async saveCode() {
      if (!this.file || this.saving || !this.codeDirty) return;
      this.saving = true;
      this.saveError = null;
      try {
        await saveArtifact(this.file.path, this.codeContent);
        this.codeDirty = false; // 저장본 = 현재 내용 → dirty 해제
      } catch (e) {
        this.saveError = e?.message || "저장에 실패했습니다.";
      } finally {
        this.saving = false;
      }
    },
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
        <!-- markdown 렌더/수정 토글 + 저장(수정 모드) — 옵시디언식 .md 원문 편집 -->
        <template v-if="file && mode === 'markdown'">
          <div class="inline-flex items-center gap-0.5 rounded-lg border border-line bg-[#F7F7F8] p-0.5">
            <button @click="setMdTab('render')" class="rounded-md px-2.5 py-1 text-[11.5px] font-bold" :class="mdTab === 'render' ? 'bg-amber text-white' : 'text-ink-600'">렌더</button>
            <button @click="setMdTab('edit')" class="rounded-md px-2.5 py-1 text-[11.5px] font-bold" :class="mdTab === 'edit' ? 'bg-amber text-white' : 'text-ink-600'">수정</button>
          </div>
          <button
            v-if="mdTab === 'edit'"
            @click="save"
            :disabled="!canSave"
            class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-semibold transition-colors"
            :class="canSave ? 'bg-amber text-white hover:bg-amber-600' : 'cursor-not-allowed bg-[#F4F4F6] text-ink-400'"
            title="저장 (Ctrl/Cmd+S)"
          >
            <Icon name="check" :size="14" />{{ saving ? "저장 중…" : "저장" }}
          </button>
        </template>
        <!-- code: 단일 편집화면(렌더/수정 탭 없음) — 저장 버튼만 -->
        <button
          v-if="file && mode === 'code'"
          @click="saveCode"
          :disabled="!codeCanSave"
          class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-semibold transition-colors"
          :class="codeCanSave ? 'bg-amber text-white hover:bg-amber-600' : 'cursor-not-allowed bg-[#F4F4F6] text-ink-400'"
          title="저장 (Ctrl/Cmd+S)"
        >
          <Icon name="check" :size="14" />{{ saving ? "저장 중…" : "저장" }}
        </button>
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
        <!-- markdown: 렌더 / 수정(옵시디언식 .md 원문 편집).
             렌더 div 는 v-show 로 DOM 에 유지 → mermaid 변환 결과 보존(탭 전환 시 재변환 비용 없음) -->
        <template v-if="mode === 'markdown'">
          <div
            v-show="mdTab === 'render'"
            ref="mdBody"
            class="md-body h-full overflow-y-auto nice-scroll"
            :class="big ? 'px-10 py-7 lg:px-16' : 'px-[22px] py-5'"
            v-html="mdHtml"
          ></div>
          <div v-show="mdTab === 'edit'" class="flex h-full min-h-0 flex-col">
            <!-- Milkdown Crepe(WYSIWYG) 호스트. 렌더된 채로 클릭해 그 자리 편집.
                 v-show 로 호스트 DOM 을 유지(비동기 destroy 레이스 방어). -->
            <div class="relative min-h-0 flex-1 overflow-hidden">
              <div
                ref="mdEditor"
                class="md-wysiwyg h-full overflow-y-auto nice-scroll"
                :class="big ? 'px-6 py-4 lg:px-10' : 'px-2 py-2'"
              ></div>
              <div
                v-if="editorLoading"
                class="absolute inset-0 flex items-center justify-center bg-white/70 text-[13px] text-ink-400"
              >에디터 불러오는 중…</div>
            </div>
            <!-- 상태 바: 저장 오류 또는 dirty/단축키 안내 -->
            <div
              v-if="saveError"
              class="flex items-center gap-1.5 border-t border-line-soft bg-[#FCEEEE] px-4 py-2 text-[12px] font-semibold text-red-500"
            >
              <Icon name="alert" :size="14" />{{ saveError }}
            </div>
            <div
              v-else
              class="flex items-center justify-between border-t border-line-soft bg-[#FAFAFB] px-4 py-1.5 text-[11px] text-ink-400"
            >
              <span :class="dirty ? 'font-semibold text-amber-600' : ''">{{ dirty ? "● 저장되지 않은 변경" : "변경 없음" }}</span>
              <span>WYSIWYG · Ctrl/Cmd+S 로 저장</span>
            </div>
          </div>
        </template>

        <!-- code: CodeMirror 6 신택스 하이라이팅 + 편집(단일 화면). lazy 로드.
             :key 로 파일 전환 시 깔끔히 remount(스테일 상태 방지). -->
        <div v-else-if="mode === 'code'" class="flex h-full min-h-0 flex-col">
          <div class="relative min-h-0 flex-1 overflow-hidden">
            <CodeMirrorEditor
              :key="file.path"
              :content="codeContent"
              :language-hint="codeLanguageHint"
              :extension="codeExtension"
              :readonly="codeReadonly"
              theme="light"
              @update:content="onCodeInput"
              @save="saveCode"
            />
          </div>
          <!-- 상태 바: 저장 오류 또는 dirty/단축키 안내 -->
          <div
            v-if="saveError"
            class="flex items-center gap-1.5 border-t border-line-soft bg-[#FCEEEE] px-4 py-2 text-[12px] font-semibold text-red-500"
          >
            <Icon name="alert" :size="14" />{{ saveError }}
          </div>
          <div
            v-else
            class="flex items-center justify-between border-t border-line-soft bg-[#FAFAFB] px-4 py-1.5 text-[11px] text-ink-400"
          >
            <span :class="codeDirty ? 'font-semibold text-amber-600' : ''">{{ codeReadonly ? "읽기 전용(오프라인)" : codeDirty ? "● 저장되지 않은 변경" : "변경 없음" }}</span>
            <span>코드 · Ctrl/Cmd+S 로 저장</span>
          </div>
        </div>

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

<style scoped>
/* Milkdown Crepe(WYSIWYG) 를 모니터 디자인 토큰(Pretendard·앰버 액센트)에 맞춰 정렬.
   frame 테마의 기본 폰트/회색 액센트를 우리 토큰으로 덮어쓴다. :deep 로 에디터 내부 타깃. */
.md-wysiwyg :deep(.milkdown) {
  height: 100%;
  --crepe-font-default: "Pretendard", system-ui, -apple-system, sans-serif;
  --crepe-font-title: "Pretendard", system-ui, -apple-system, sans-serif;
  --crepe-color-primary: #dd6b1f; /* amber DEFAULT */
  --crepe-color-on-background: #1a1a1e; /* ink-900 */
  --crepe-color-inline-code: #c2570b; /* amber-600 */
}
/* 편집 영역: 컨테이너를 꽉 채우고, 빈 영역 클릭으로도 포커스되도록 최소 높이 확보 */
.md-wysiwyg :deep(.milkdown .ProseMirror) {
  min-height: 100%;
  padding: 4px 6px 48px;
  outline: none;
}
</style>
