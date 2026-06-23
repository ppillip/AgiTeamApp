<script>
import Icon from "./Icon.vue";
import {
  store,
  closeContextMenu,
  copyArtifactPath,
  deleteArtifact,
  openCreateFileDialog,
  uploadArtifactFiles,
  downloadArtifact,
} from "../stores/monitor.js";

// 산출물 트리 우클릭 컨텍스트 메뉴(WG-ART-07 + DS-132 §10). 트리 노드 @contextmenu → store.contextMenu.
//  - position:fixed + 뷰포트 보정(화면 밖으로 안 넘치게 좌/상 반전).
//  - 빈 영역 클릭 / 스크롤 / ESC / 창 리사이즈 시 닫힘.
//  - 폴더(isDir): [새로만들기][파일업로드] + 경로 복사 + 삭제
//  - 파일      : [다운로드] + 경로 복사 + 삭제
const MENU_W = 184; // 메뉴 추정 폭(보정 계산용)
const ITEM_H = 34; // 항목 1개 추정 높이(세로 보정 계산용)
const HEADER_H = 24; // 대상 이름 헤더 추정 높이

export default {
  name: "ArtifactContextMenu",
  components: { Icon },
  computed: {
    store: () => store,
    menu() {
      return store.contextMenu;
    },
    // 항목 수(폴더=새로만들기+업로드+복사+삭제=4, 파일=다운로드+복사+삭제=3)로 메뉴 높이 추정.
    menuH() {
      const items = this.isDir ? 4 : 3;
      return HEADER_H + items * ITEM_H + 8;
    },
    // 뷰포트 경계 보정: 우/하단 넘침이면 커서 기준 왼쪽/위쪽으로 펼친다.
    pos() {
      const pad = 8;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let x = this.menu.x;
      let y = this.menu.y;
      if (x + MENU_W + pad > vw) x = Math.max(pad, vw - MENU_W - pad);
      if (y + this.menuH + pad > vh) y = Math.max(pad, vh - this.menuH - pad);
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
    // 새로만들기(폴더 한정): 파일명 입력 다이얼로그를 연다(store.createDialog).
    onCreate() {
      openCreateFileDialog(this.menu.node);
    },
    // 파일업로드(폴더 한정): 로컬 파일 picker 를 띄우고 선택 파일을 순차 업로드한다.
    //   메뉴는 먼저 닫고(노드 보존) input 을 동적으로 생성·클릭한다(다중 선택 허용).
    onUpload() {
      const node = this.menu.node;
      closeContextMenu();
      if (!node) return;
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.style.display = "none";
      input.onchange = () => {
        const files = Array.from(input.files || []);
        if (files.length) uploadArtifactFiles(node, files);
        input.remove();
      };
      document.body.appendChild(input);
      input.click();
    },
    // 다운로드(파일 한정): BE stream(download=1) URL 을 anchor 로 트리거.
    onDownload() {
      downloadArtifact(this.menu.node);
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
      <!-- 폴더 전용: 새로만들기 / 파일업로드 (DS-132 §10) -->
      <template v-if="isDir">
        <button
          @click="onCreate"
          class="flex w-full items-center gap-2.5 px-3 py-[7px] text-left text-[13px] text-ink-700 hover:bg-[#F4F4F6]"
        >
          <Icon name="plus" :size="15" class="flex-shrink-0 text-ink-500" />
          새로만들기
        </button>
        <button
          @click="onUpload"
          class="flex w-full items-center gap-2.5 px-3 py-[7px] text-left text-[13px] text-ink-700 hover:bg-[#F4F4F6]"
        >
          <Icon name="paperclip" :size="15" class="flex-shrink-0 text-ink-500" />
          파일업로드
        </button>
      </template>
      <!-- 파일 전용: 다운로드 (DS-132 §10) -->
      <button
        v-else
        @click="onDownload"
        class="flex w-full items-center gap-2.5 px-3 py-[7px] text-left text-[13px] text-ink-700 hover:bg-[#F4F4F6]"
      >
        <Icon name="download" :size="15" class="flex-shrink-0 text-ink-500" />
        다운로드
      </button>
      <!-- 구분선 -->
      <div class="my-1 border-t border-line-soft"></div>
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
