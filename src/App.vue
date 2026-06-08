<script>
import Icon from "./components/Icon.vue";
import ProjectSwitcher from "./components/ProjectSwitcher.vue";
import ConversationList from "./components/ConversationList.vue";
import ChatView from "./components/ChatView.vue";
import ArtifactPanel from "./components/ArtifactPanel.vue";
import { store, boot, teardown, selectedProject } from "./stores/monitor.js";

// 메인 셸 (S-01): 헤더(프로젝트 선택·연결상태) + 좌(채팅방)·중(대화)·우(산출물) 3분할.
// 모든 데이터는 선택 project_id 기준(store). 백엔드 미연결 시 degraded(목업) 배너 표시.
//
// UI-01: 좌/우 패널 경계를 드래그해 폭 조절(resizable splitter). 중앙(대화)은 나머지(flex-1)를
// 채운다. 조절한 폭은 localStorage 에 저장해 새로고침 후에도 유지한다.
const PANEL_W_KEY = "agiteamapp.panelW";
const LEFT_MIN = 220, LEFT_MAX = 560; // 채팅방 패널 폭 한계
const RIGHT_MIN = 300, RIGHT_MAX = 820; // 산출물 패널 폭 한계
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export default {
  name: "App",
  components: { Icon, ProjectSwitcher, ConversationList, ChatView, ArtifactPanel },
  data() {
    return { leftW: 316, rightW: 400, drag: null };
  },
  computed: {
    store: () => store,
    project() {
      return selectedProject();
    },
    pmConnected() {
      return !!this.project && this.project.pmConnected;
    },
  },
  methods: {
    // 드래그 시작: 어느 경계(left|right)인지, 시작 X·시작 폭 기록 후 전역 리스너 부착.
    startDrag(side, e) {
      this.drag = {
        side,
        startX: e.clientX,
        startW: side === "left" ? this.leftW : this.rightW,
      };
      window.addEventListener("mousemove", this.onDrag);
      window.addEventListener("mouseup", this.endDrag);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
    },
    onDrag(e) {
      if (!this.drag) return;
      const dx = e.clientX - this.drag.startX;
      if (this.drag.side === "left") {
        // 좌측 경계: 오른쪽으로 끌면 채팅방 패널이 넓어짐
        this.leftW = clamp(this.drag.startW + dx, LEFT_MIN, LEFT_MAX);
      } else {
        // 우측 경계: 왼쪽으로 끌면 산출물 패널이 넓어짐(부호 반대)
        this.rightW = clamp(this.drag.startW - dx, RIGHT_MIN, RIGHT_MAX);
      }
    },
    endDrag() {
      this.drag = null;
      window.removeEventListener("mousemove", this.onDrag);
      window.removeEventListener("mouseup", this.endDrag);
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      this.persistWidths();
    },
    persistWidths() {
      try {
        localStorage.setItem(
          PANEL_W_KEY,
          JSON.stringify({ left: this.leftW, right: this.rightW })
        );
      } catch {}
    },
    restoreWidths() {
      try {
        const raw = localStorage.getItem(PANEL_W_KEY);
        if (!raw) return;
        const v = JSON.parse(raw);
        if (Number.isFinite(v?.left)) this.leftW = clamp(v.left, LEFT_MIN, LEFT_MAX);
        if (Number.isFinite(v?.right)) this.rightW = clamp(v.right, RIGHT_MIN, RIGHT_MAX);
      } catch {}
    },
  },
  mounted() {
    this.restoreWidths();
    boot();
  },
  beforeUnmount() {
    window.removeEventListener("mousemove", this.onDrag);
    window.removeEventListener("mouseup", this.endDrag);
    teardown();
  },
};
</script>

<template>
  <div class="flex h-full flex-col bg-[#F4F4F6] text-ink-900">
    <!-- 헤더 -->
    <header class="flex h-[62px] flex-shrink-0 items-center justify-between border-b border-line bg-white px-[22px]">
      <div class="flex items-center gap-4">
        <div class="flex items-center gap-[11px]">
          <div class="flex h-[34px] w-[34px] items-center justify-center rounded-[10px] bg-amber text-[17px] font-extrabold text-white">A</div>
          <div>
            <div class="text-[15px] font-bold tracking-[-0.01em]">AgiTeamApp</div>
            <div class="mt-px text-[11.5px] text-ink-500">팀 모니터 · 대화와 산출물</div>
          </div>
        </div>
        <div class="mx-1 h-7 w-px bg-line"></div>
        <ProjectSwitcher />
      </div>

      <div class="flex items-center gap-2.5">
        <!-- 전역 상태 3종 (DS-60 §4.4): MOCK(목업) · LIVE(실연결) · 끊김. 실데이터 위장 금지. -->
        <span
          v-if="store.degraded"
          class="flex items-center gap-[7px] rounded-[9px] border border-amber-tintbd bg-amber-tint px-[13px] py-[7px] text-[12.5px] font-bold tracking-wide text-amber-600"
          :title="store.bootError ? ('목업 표시 중 · ' + store.bootError) : '백엔드 미연결 — 목업(샘플) 데이터'"
        >
          <Icon name="alert" :size="14" />MOCK · 목업
        </span>
        <span
          v-else
          class="flex items-center gap-[7px] rounded-[9px] border px-[13px] py-[7px] text-[13px] font-bold tracking-wide"
          :class="pmConnected ? 'border-grn-tintbd bg-grn-tint text-grn' : 'border-line bg-line-soft text-ink-500'"
          :title="pmConnected ? 'PM 방 실시간 연결됨' : 'PM surface 미발견(끊김)'"
        >
          <span class="h-[7px] w-[7px] rounded-full" :class="pmConnected ? 'bg-grn ring-[3px] ring-grn/20' : 'bg-ink-300'"></span>
          {{ pmConnected ? "LIVE" : "끊김" }} · PM
        </span>
      </div>
    </header>

    <!-- 3분할 본문: 좌/우 패널은 드래그로 폭 조절(UI-01). 중앙(대화)은 나머지(flex-1)를 채우며
         최소폭을 보장(좁은 화면에서 말풍선 세로 잘림 방지) + 그 이하 폭에선 가로 스크롤 폴백 -->
    <div class="flex min-h-0 flex-1 overflow-x-auto p-[14px]">
      <!-- 좌: 채팅방 (가변폭) -->
      <div class="min-h-0 flex-shrink-0" :style="{ width: leftW + 'px' }">
        <ConversationList />
      </div>
      <!-- 좌↔중 splitter -->
      <div
        class="group flex w-[14px] flex-shrink-0 cursor-col-resize items-center justify-center"
        @mousedown.prevent="startDrag('left', $event)"
        title="드래그하여 폭 조절"
      >
        <div class="h-10 w-[3px] rounded-full bg-line transition-colors group-hover:bg-amber"
             :class="drag && drag.side === 'left' ? 'bg-amber' : ''"></div>
      </div>
      <!-- 중: 대화 -->
      <ChatView />
      <!-- 중↔우 splitter -->
      <div
        class="group flex w-[14px] flex-shrink-0 cursor-col-resize items-center justify-center"
        @mousedown.prevent="startDrag('right', $event)"
        title="드래그하여 폭 조절"
      >
        <div class="h-10 w-[3px] rounded-full bg-line transition-colors group-hover:bg-amber"
             :class="drag && drag.side === 'right' ? 'bg-amber' : ''"></div>
      </div>
      <!-- 우: 산출물 (가변폭) -->
      <div class="min-h-0 flex-shrink-0" :style="{ width: rightW + 'px' }">
        <ArtifactPanel />
      </div>
    </div>
  </div>
</template>
