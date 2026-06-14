<script>
import Icon from "./Icon.vue";
import { store, toggleFolder, childrenOf, openFile } from "../stores/monitor.js";
import { folderHasUnseenChange } from "../stores/artifactChange.js";

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
    // 현재 활성 탭(root_type)의 외부변경 맵. 트리는 항상 활성 탭 트리이므로 그 탭의 표식만 본다.
    rootChanges() {
      return store.externalChanges[store.rootType] || {};
    },
    // 외부 수정 표식(WG-ART-06): artifact_watcher 가 감지한 외부 변경 '파일' → 파일명 amber 강조.
    // 파일을 열면(openFile) 해제된다. (root_type 별 분리 — 코드/페르소나 탭도 동일 동작)
    externallyChanged() {
      return !this.node.isDir && !!this.rootChanges[this.node.path];
    },
    // UI-10 폴더 전파(요구사항 17-2): 폴더 하위에 미열람 변경이 있으면 폴더명도 amber.
    // 접힌 폴더 안의 변경도 펼치지 않고 인지 가능. 형제 변경이 남아 있으면 유지, 다 열람되면 원복.
    folderHasUnseen() {
      return this.node.isDir && folderHasUnseenChange(this.rootChanges, this.node.path);
    },
    // 파일/폴더 공통 미열람 강조 여부(파일=자신 변경, 폴더=하위 변경 전파)
    markUnseen() {
      return this.node.isDir ? this.folderHasUnseen : this.externallyChanged;
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
        <Icon name="folder" :size="15" class="flex-shrink-0" :class="selected || markUnseen ? 'text-amber' : 'text-ink-300'" />
      </template>
      <template v-else>
        <span class="w-3.5 flex-shrink-0"></span>
        <Icon name="file" :size="15" class="flex-shrink-0" :class="selected || markUnseen ? 'text-amber' : 'text-ink-300'" />
      </template>
      <span
        class="truncate"
        :class="markUnseen && !selected ? 'font-semibold text-amber-600' : ''"
      >{{ node.name }}</span>
      <!-- 미열람 변경 표식 점(amber). 파일=자신 변경 / 폴더=하위 변경 전파(UI-10).
           후행 그룹(점+배지)을 우측으로 미는 ml-auto 담당. -->
      <span
        v-if="markUnseen"
        class="ml-auto h-1.5 w-1.5 flex-shrink-0 rounded-full bg-amber"
        :title="node.isDir ? '하위에 미열람 변경이 있습니다 — 펼쳐서 확인하세요' : '외부에서 수정됨 — 열어서 확인하세요'"
      ></span>
      <span
        v-if="!node.isDir && node.ext"
        class="flex-shrink-0 rounded bg-line-soft px-1.5 py-px text-[9.5px] font-bold text-ink-500"
        :class="markUnseen ? 'ml-1.5' : 'ml-auto'"
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
