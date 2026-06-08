<script>
import Icon from "./Icon.vue";
import ArtifactTree from "./ArtifactTree.vue";
import ArtifactViewer from "./ArtifactViewer.vue";
import { store, loadTreeRoot } from "../stores/monitor.js";

// 우측 산출물 패널: 상단 트리(S-04) + 하단 뷰어(S-05).
// UI-05: 트리↔뷰어 경계를 드래그해 세로 분할 높이 조절(localStorage 유지). UI-01 splitter 패턴 재사용.
const TREE_H_KEY = "agiteamapp.treeH";
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export default {
  name: "ArtifactPanel",
  components: { Icon, ArtifactTree, ArtifactViewer },
  // treeOnly: '크게'(인라인 확대) 모드에서 우측 패널을 트리만 표시(작은 뷰어 숨김).
  props: { treeOnly: { type: Boolean, default: false } },
  emits: ["expand"],
  data() {
    return { treeH: 240, dragging: false, _startY: 0, _startH: 0, _panelH: 0 };
  },
  computed: {
    store: () => store,
    rootChildren() {
      return store.treeRoot?.children || [];
    },
  },
  methods: {
    reload() {
      loadTreeRoot();
    },
    startDrag(e) {
      this.dragging = true;
      this._startY = e.clientY;
      this._startH = this.treeH;
      this._panelH = this.$el ? this.$el.clientHeight : 600;
      window.addEventListener("mousemove", this.onDrag);
      window.addEventListener("mouseup", this.endDrag);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "row-resize";
    },
    onDrag(e) {
      if (!this.dragging) return;
      const dy = e.clientY - this._startY;
      // 뷰어 최소 높이(~220px) 확보하면서 트리 높이 조절
      const max = Math.max(160, this._panelH - 220);
      this.treeH = clamp(this._startH + dy, 120, max);
    },
    endDrag() {
      this.dragging = false;
      window.removeEventListener("mousemove", this.onDrag);
      window.removeEventListener("mouseup", this.endDrag);
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      try {
        localStorage.setItem(TREE_H_KEY, String(Math.round(this.treeH)));
      } catch {}
    },
    restoreTreeH() {
      try {
        const v = parseInt(localStorage.getItem(TREE_H_KEY), 10);
        if (Number.isFinite(v)) this.treeH = clamp(v, 120, 800);
      } catch {}
    },
  },
  mounted() {
    this.restoreTreeH();
  },
  beforeUnmount() {
    window.removeEventListener("mousemove", this.onDrag);
    window.removeEventListener("mouseup", this.endDrag);
  },
};
</script>

<template>
  <aside class="flex h-full w-full flex-col overflow-hidden rounded-2xl border border-line bg-white">
    <div class="flex items-center justify-between px-[18px] pb-3 pt-[18px]">
      <h2 class="text-[16px] font-bold">산출물</h2>
      <button @click="reload" class="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-ink-500 hover:bg-[#F4F4F6] hover:text-ink-600" title="새로고침">
        <Icon name="refresh" :size="15" />
      </button>
    </div>

    <!-- 트리 (S-04): 높이는 드래그로 조절(UI-05). 큰 뷰 모드(treeOnly)에선 트리가 패널 전체 차지 -->
    <div
      class="overflow-y-auto border-b border-line-soft px-3 pb-3 nice-scroll"
      :class="treeOnly ? 'min-h-0 flex-1' : 'flex-shrink-0'"
      :style="treeOnly ? null : { height: treeH + 'px' }"
    >
      <div v-if="store.treeLoading" class="px-2 py-3 text-[13px] text-ink-400">트리 불러오는 중…</div>
      <div v-else-if="!rootChildren.length" class="px-2 py-3 text-[13px] text-ink-400">산출물이 없습니다.</div>
      <ArtifactTree
        v-for="child in rootChildren"
        :key="child.path"
        :node="child"
        :depth="0"
      />
    </div>

    <!-- 트리↔뷰어 splitter (세로 리사이즈, UI-05) — 큰 뷰 모드에선 숨김 -->
    <div
      v-if="!treeOnly"
      class="group flex h-2.5 flex-shrink-0 cursor-row-resize items-center justify-center border-b border-line-soft"
      @mousedown.prevent="startDrag"
      title="드래그하여 트리/미리보기 높이 조절"
    >
      <div class="h-[3px] w-9 rounded-full bg-line transition-colors group-hover:bg-amber" :class="dragging ? 'bg-amber' : ''"></div>
    </div>

    <!-- 뷰어 (S-05) — 큰 뷰 모드에선 중앙(채팅 영역)으로 이동하므로 패널에선 숨김. '크게'는 전파. -->
    <ArtifactViewer v-if="!treeOnly" @expand="$emit('expand')" />
  </aside>
</template>
