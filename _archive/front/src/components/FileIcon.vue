<script>
import { fileIconSvg, folderIconSvg } from "../lib/materialIcons.js";

// 파일트리 전용 Material Icon (컬러 SVG). Lucide 라인 아이콘과 별개 — 트리에서만 사용.
// 출처: material-icon-theme (MIT). 매핑/경량화 근거는 ../lib/materialIcons.js 참조.
export default {
  name: "FileIcon",
  props: {
    name: { type: String, default: "" },
    ext: { type: String, default: "" },
    isDir: { type: Boolean, default: false },
    isOpen: { type: Boolean, default: false }, // 폴더 펼침 → 열린 폴더 아이콘
    size: { type: Number, default: 16 },
  },
  computed: {
    svg() {
      return this.isDir ? folderIconSvg(this.name, this.isOpen) : fileIconSvg(this.name, this.ext);
    },
  },
};
</script>

<template>
  <span
    class="inline-flex flex-shrink-0 items-center justify-center"
    :style="{ width: size + 'px', height: size + 'px' }"
    v-html="svg"
  ></span>
</template>
