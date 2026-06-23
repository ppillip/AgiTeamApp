<script>
import Icon from "./Icon.vue";
import { store, closeCreateFileDialog, submitCreateFile } from "../stores/monitor.js";

// 새로만들기 다이얼로그(WG-ART-08, DS-132 §4): 폴더 우클릭 → 파일명(확장자 포함) 입력 모달.
//  - Enter 제출 / ESC·취소 닫힘. busy 중에는 입력·제출 잠금.
//  - 에러는 store.createDialog.error 인라인 표시(409 중복 → 이름 변경 유도).
export default {
  name: "ArtifactCreateDialog",
  components: { Icon },
  computed: {
    store: () => store,
    d() {
      return store.createDialog;
    },
  },
  methods: {
    close() {
      if (this.d.busy) return;
      closeCreateFileDialog();
    },
    submit() {
      if (this.d.busy) return;
      submitCreateFile();
    },
  },
  watch: {
    // 열릴 때 입력에 포커스(마이크로태스크 뒤 DOM 준비 후).
    "store.createDialog.open"(open) {
      if (open) {
        this.$nextTick(() => {
          const el = this.$refs.input;
          if (el) el.focus();
        });
      }
    },
  },
};
</script>

<template>
  <div
    v-if="d.open"
    class="fixed inset-0 z-[80] flex items-center justify-center bg-black/30 px-4"
    @click.self="close"
    @keydown.esc="close"
  >
    <div class="w-full max-w-[380px] overflow-hidden rounded-[14px] border border-line bg-white shadow-[0_16px_48px_rgba(0,0,0,0.22)]">
      <div class="flex items-center gap-2 px-4 pt-4 pb-1">
        <Icon name="plus" :size="16" class="text-amber-600" />
        <h3 class="text-[14px] font-bold text-ink-800">새 파일 만들기</h3>
      </div>
      <p class="truncate px-4 pb-3 text-[12px] text-ink-400" :title="d.parentPath">
        위치: {{ d.parentName || "루트" }}
      </p>
      <div class="px-4">
        <input
          ref="input"
          v-model="d.filename"
          type="text"
          placeholder="파일명.확장자 (예: 새문서.md)"
          :disabled="d.busy"
          @keydown.enter.prevent="submit"
          class="w-full rounded-[9px] border border-line px-3 py-2 text-[13px] text-ink-800 outline-none focus:border-amber disabled:opacity-60"
        />
        <p v-if="d.error" class="mt-2 text-[12px] font-semibold text-red-600">{{ d.error }}</p>
        <p v-else class="mt-2 text-[11.5px] text-ink-400">허용 확장자만 생성됩니다(md, json, txt, 코드 등).</p>
      </div>
      <div class="flex justify-end gap-2 px-4 pb-4 pt-3">
        <button
          @click="close"
          :disabled="d.busy"
          class="rounded-[9px] px-3 py-[7px] text-[13px] font-semibold text-ink-600 hover:bg-[#F4F4F6] disabled:opacity-60"
        >
          취소
        </button>
        <button
          @click="submit"
          :disabled="d.busy || !d.filename.trim()"
          class="rounded-[9px] bg-amber-600 px-3.5 py-[7px] text-[13px] font-semibold text-white hover:bg-amber-700 disabled:opacity-50"
        >
          {{ d.busy ? "만드는 중…" : "만들기" }}
        </button>
      </div>
    </div>
  </div>
</template>
