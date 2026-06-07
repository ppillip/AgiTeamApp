<script>
import Icon from "./Icon.vue";
import { store, closeViewer } from "../stores/monitor.js";
import { fileStreamUrl } from "../api/index.js";
import { renderMarkdown } from "../lib/markdown.js";

// 산출물 뷰어 (S-05, WG-ART-02/03). render_mode 별 분기:
//  - markdown        → 경량 MD 렌더(무의존, XSS 안전)
//  - pdf_stream      → stream_url 을 iframe 으로 임베드
//  - converted_preview(pptx/docx) → 변환 대기/미지원 안내 + 다운로드 유도
//  - unsupported     → 미지원 안내
export default {
  name: "ArtifactViewer",
  components: { Icon },
  data() {
    return { fullscreen: false };
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
      return this.file?.renderMode || null;
    },
    html() {
      if (this.mode === "markdown" && this.file?.content != null) {
        return renderMarkdown(this.file.content);
      }
      return "";
    },
    pdfUrl() {
      if (this.mode !== "pdf_stream" || !this.file) return null;
      // 백엔드가 stream_url 을 주면 그대로, 아니면 path 로 구성
      return this.file.streamUrl || fileStreamUrl(this.file.path, "original");
    },
    extBadge() {
      return (this.file?.ext || "").toUpperCase();
    },
    downloadHref() {
      if (!this.file) return null;
      return this.file.streamUrl || fileStreamUrl(this.file.path, "original");
    },
  },
  methods: {
    closeViewer,
    toggleFull() {
      this.fullscreen = !this.fullscreen;
    },
  },
};
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col p-4">
    <!-- 헤더 -->
    <div class="mb-3 flex items-center justify-between gap-2">
      <div class="flex min-w-0 items-center gap-[9px] text-[13.5px] font-semibold">
        <span v-if="file" class="flex-shrink-0 rounded-md bg-amber-tint px-1.5 py-[3px] text-[10px] font-extrabold text-amber-600">{{ extBadge || "DOC" }}</span>
        <span class="truncate">{{ file ? file.name : "산출물 뷰어" }}</span>
      </div>
      <div class="flex flex-shrink-0 items-center gap-1">
        <button v-if="file" @click="toggleFull" class="rounded-lg bg-[#F4F4F6] px-[13px] py-1.5 text-[12.5px] font-semibold text-ink-600 hover:bg-line-soft">크게</button>
        <button v-if="v.open" @click="closeViewer" class="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-ink-500 hover:bg-[#F4F4F6]"><Icon name="x" :size="16" /></button>
      </div>
    </div>

    <!-- 본문 -->
    <div class="relative flex-1 overflow-hidden rounded-[13px] border border-line bg-white">
      <!-- 안내(파일 미선택) -->
      <div v-if="!v.open" class="flex h-full items-center justify-center px-6 text-center text-[13px] text-ink-400">
        좌측 트리에서 파일을 클릭하면<br />여기에 내용이 표시됩니다.
      </div>
      <!-- 로딩 -->
      <div v-else-if="v.loading" class="flex h-full items-center justify-center text-[13px] text-ink-400">불러오는 중…</div>
      <!-- 에러 -->
      <div v-else-if="v.error" class="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <Icon name="alert" :size="22" class="text-red-400" />
        <div class="text-[13px] font-semibold text-red-500">{{ v.error }}</div>
      </div>

      <template v-else-if="file">
        <!-- markdown -->
        <div v-if="mode === 'markdown'" class="md-body h-full overflow-y-auto px-[22px] py-5 nice-scroll" v-html="html"></div>

        <!-- pdf -->
        <iframe
          v-else-if="mode === 'pdf_stream' && pdfUrl"
          :src="pdfUrl"
          class="h-full w-full border-0"
          title="PDF 미리보기"
        ></iframe>

        <!-- pptx / docx 변환 대기 또는 미지원 -->
        <div v-else class="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
          <Icon name="fileText" :size="30" class="text-ink-300" />
          <div class="text-[14px] font-semibold text-ink-700">{{ file.name }}</div>
          <div class="text-[13px] text-ink-400" v-if="mode === 'converted_preview'">
            {{ file.ext ? file.ext.toUpperCase() : "문서" }} 미리보기 변환을 준비 중입니다.<br />변환이 완료되면 이곳에 렌더됩니다.
          </div>
          <div class="text-[13px] text-ink-400" v-else>
            이 형식은 미리보기를 지원하지 않습니다.
          </div>
          <a
            v-if="downloadHref"
            :href="downloadHref"
            target="_blank"
            rel="noopener noreferrer"
            class="mt-1 inline-flex items-center gap-1.5 rounded-[10px] border border-line px-3.5 py-2 text-[13px] font-semibold text-ink-600 hover:bg-[#F4F4F6]"
          >
            <Icon name="download" :size="15" />원본 열기
          </a>
        </div>
      </template>
    </div>

    <!-- 풀스크린 오버레이 -->
    <Teleport to="body">
      <div v-if="fullscreen && file" class="fixed inset-0 z-50 flex flex-col bg-black/40 p-6" @click.self="toggleFull">
        <div class="mx-auto flex h-full w-full max-w-[980px] flex-col overflow-hidden rounded-2xl border border-line bg-white shadow-2xl">
          <div class="flex items-center justify-between border-b border-line-soft px-5 py-3.5">
            <div class="flex items-center gap-2 text-[14px] font-semibold">
              <span class="rounded-md bg-amber-tint px-1.5 py-[3px] text-[10px] font-extrabold text-amber-600">{{ extBadge || "DOC" }}</span>
              {{ file.name }}
            </div>
            <button @click="toggleFull" class="flex h-8 w-8 items-center justify-center rounded-lg text-ink-500 hover:bg-[#F4F4F6]"><Icon name="x" :size="18" /></button>
          </div>
          <div class="min-h-0 flex-1 overflow-hidden bg-white">
            <div v-if="mode === 'markdown'" class="md-body h-full overflow-y-auto px-10 py-7 nice-scroll" v-html="html"></div>
            <iframe v-else-if="mode === 'pdf_stream' && pdfUrl" :src="pdfUrl" class="h-full w-full border-0" title="PDF 미리보기"></iframe>
            <div v-else class="flex h-full items-center justify-center text-[13px] text-ink-400">미리보기를 표시할 수 없습니다.</div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
