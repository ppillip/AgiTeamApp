<script>
import Icon from "./Icon.vue";
import { store, toggleFolder, childrenOf, openFile } from "../stores/monitor.js";

// 산출물 트리 노드 (S-04, WG-ART-01) — 재귀 컴포넌트.
//  - 폴더: 클릭 → 펼침/접힘(lazy 로드). 파일: 클릭 → 뷰어(S-05) 로드.
//  - depth 만큼 들여쓰기. children 은 store 캐시(childrenCache) 또는 node.children.
export default {
  name: "ArtifactTree",
  components: { Icon },
  props: {
    node: { type: Object, required: true },
    depth: { type: Number, default: 0 },
  },
  computed: {
    store: () => store,
    isOpen() {
      return !!store.expanded[this.node.path];
    },
    children() {
      return childrenOf(this.node);
    },
    loading() {
      return !!store.childrenLoading[this.node.path];
    },
    selected() {
      return store.viewer.path === this.node.path;
    },
  },
  methods: {
    onClick() {
      if (this.node.isDir) toggleFolder(this.node);
      else openFile(this.node);
    },
    extBadge(ext) {
      return (ext || "").toUpperCase();
    },
  },
};
</script>

<template>
  <div>
    <button
      @click="onClick"
      :class="['flex w-full items-center gap-[7px] rounded-[9px] px-2 py-[7px] text-left text-[13.5px]',
               selected ? 'bg-amber-tint font-semibold text-amber-600' : 'text-ink-700 hover:bg-[#F4F4F6]']"
      :style="{ paddingLeft: (8 + depth * 14) + 'px' }"
    >
      <template v-if="node.isDir">
        <Icon :name="isOpen ? 'chevronDown' : 'chevronRight'" :size="14" class="flex-shrink-0 text-ink-500" />
        <Icon name="folder" :size="15" class="flex-shrink-0" :class="selected ? 'text-amber' : 'text-ink-300'" />
      </template>
      <template v-else>
        <span class="w-3.5 flex-shrink-0"></span>
        <Icon name="file" :size="15" class="flex-shrink-0" :class="selected ? 'text-amber' : 'text-ink-300'" />
      </template>
      <span class="truncate">{{ node.name }}</span>
      <span
        v-if="!node.isDir && node.ext"
        class="ml-auto flex-shrink-0 rounded bg-line-soft px-1.5 py-px text-[9.5px] font-bold text-ink-500"
      >{{ extBadge(node.ext) }}</span>
    </button>

    <div v-if="node.isDir && isOpen">
      <div v-if="loading" class="py-1.5 text-[12px] text-ink-400" :style="{ paddingLeft: (8 + (depth + 1) * 14 + 14) + 'px' }">불러오는 중…</div>
      <ArtifactTree
        v-for="child in children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
      />
      <div
        v-if="!loading && !children.length"
        class="py-1.5 text-[12px] text-ink-300"
        :style="{ paddingLeft: (8 + (depth + 1) * 14 + 14) + 'px' }"
      >비어 있음</div>
    </div>
  </div>
</template>
