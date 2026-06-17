<script>
import Icon from "./Icon.vue";
import { store, closeContextMenu, copyArtifactPath, deleteArtifact } from "../stores/monitor.js";

// 산출물 트리 우클릭 컨텍스트 메뉴(WG-ART-07). 트리 노드의 @contextmenu → store.contextMenu 로 열린다.
//  - position:fixed + 뷰포트 보정(화면 밖으로 안 넘치게 좌/상 반전).
//  - 빈 영역 클릭 / 스크롤 / ESC / 창 리사이즈 시 닫힘.
//  - 항목: ①경로 복사(navigator.clipboard) ②삭제(확인 1회 후 deleteArtifact).
const MENU_W = 184; // 메뉴 추정 폭(보정 계산용)
const MENU_H = 88; // 메뉴 추정 높이(항목 2개)

export default {
  name: "ArtifactContextMenu",
  components: { Icon },
  computed: {
    store: () => store,
    menu() {
      return store.contextMenu;
    },
    // 뷰포트 경계 보정: 우/하단 넘침이면 커서 기준 왼쪽/위쪽으로 펼친다.
    pos() {
      const pad = 8;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let x = this.menu.x;
      let y = this.menu.y;
      if (x + MENU_W + pad > vw) x = Math.max(pad, vw - MENU_W - pad);
      if (y + MENU_H + pad > vh) y = Math.max(pad, vh - MENU_H - pad);
      return { left: x + "px", top: y + "px" };
    },
    targetName() {
      return this.menu.node?.name || "";
    },
    isDir() {
      return !!this.menu.node?.isDir;
    },
  },
  methods: {
    close() {
      closeContextMenu();
    },
    onCopy() {
      copyArtifactPath(this.menu.node);
    },
    onDelete() {
      const node = this.menu.node;
      if (!node) return;
      const kind = node.isDir ? "폴더" : "파일";
      // 실수 방지: 확인 다이얼로그 1회. 폴더는 하위 포함 경고.
      const msg = node.isDir
        ? `'${node.name}' 폴더와 하위 항목을 모두 삭제할까요?\n이 작업은 되돌릴 수 없습니다.`
        : `'${node.name}' ${kind}을(를) 삭제할까요?\n이 작업은 되돌릴 수 없습니다.`;
      // 메뉴를 먼저 닫고 confirm (메뉴가 confirm 위에 남지 않게)
      closeContextMenu();
      if (window.confirm(msg)) deleteArtifact(node);
    },
    onKey(e) {
      if (e.key === "Escape") this.close();
    },
  },
  watch: {
    // 메뉴가 열릴 때만 전역 리스너 부착(스크롤/리사이즈/ESC 닫힘).
    "store.contextMenu.open"(open) {
      if (open) {
        window.addEventListener("scroll", this.close, true);
        window.addEventListener("resize", this.close);
        window.addEventListener("keydown", this.onKey);
      } else {
        window.removeEventListener("scroll", this.close, true);
        window.removeEventListener("resize", this.close);
        window.removeEventListener("keydown", this.onKey);
      }
    },
  },
  beforeUnmount() {
    window.removeEventListener("scroll", this.close, true);
    window.removeEventListener("resize", this.close);
    window.removeEventListener("keydown", this.onKey);
  },
};
</script>

<template>
  <!-- 빈 영역 클릭 닫힘용 투명 오버레이(메뉴 자신 클릭은 stop) -->
  <div
    v-if="menu.open"
    class="fixed inset-0 z-[60]"
    @click="close"
    @contextmenu.prevent="close"
  >
    <div
      class="fixed min-w-[184px] overflow-hidden rounded-[11px] border border-line bg-white py-1 shadow-[0_8px_28px_rgba(0,0,0,0.16)]"
      :style="pos"
      @click.stop
    >
      <!-- 대상 이름 헤더 -->
      <div class="truncate px-3 pb-1.5 pt-1 text-[11px] font-semibold text-ink-400" :title="menu.node?.path">
        {{ targetName }}
      </div>
      <button
        @click="onCopy"
        class="flex w-full items-center gap-2.5 px-3 py-[7px] text-left text-[13px] text-ink-700 hover:bg-[#F4F4F6]"
      >
        <Icon name="copy" :size="15" class="flex-shrink-0 text-ink-500" />
        경로 복사
      </button>
      <button
        @click="onDelete"
        class="flex w-full items-center gap-2.5 px-3 py-[7px] text-left text-[13px] text-red-600 hover:bg-red-50"
      >
        <Icon name="trash" :size="15" class="flex-shrink-0 text-red-500" />
        삭제
      </button>
    </div>
  </div>
</template>
