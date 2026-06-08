<script>
import Icon from "./Icon.vue";
import ArtifactTree from "./ArtifactTree.vue";
import ArtifactViewer from "./ArtifactViewer.vue";
import { store, loadTreeRoot } from "../stores/monitor.js";

// 우측 산출물 패널: 상단 트리(S-04) + 하단 뷰어(S-05).
export default {
  name: "ArtifactPanel",
  components: { Icon, ArtifactTree, ArtifactViewer },
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

    <!-- 트리 (S-04) -->
    <div class="max-h-[40%] min-h-[120px] overflow-y-auto border-b border-line-soft px-3 pb-3 nice-scroll">
      <div v-if="store.treeLoading" class="px-2 py-3 text-[13px] text-ink-400">트리 불러오는 중…</div>
      <div v-else-if="!rootChildren.length" class="px-2 py-3 text-[13px] text-ink-400">산출물이 없습니다.</div>
      <ArtifactTree
        v-for="child in rootChildren"
        :key="child.path"
        :node="child"
        :depth="0"
      />
    </div>

    <!-- 뷰어 (S-05) -->
    <ArtifactViewer />
  </aside>
</template>
