<script>
import Icon from "./components/Icon.vue";
import ProjectSwitcher from "./components/ProjectSwitcher.vue";
import ConversationList from "./components/ConversationList.vue";
import ChatView from "./components/ChatView.vue";
import ArtifactPanel from "./components/ArtifactPanel.vue";
import ArtifactViewer from "./components/ArtifactViewer.vue";
import TeamView from "./components/TeamView.vue";
import { store, boot, teardown, selectedProject, selectRoom, loadRoomPreviews } from "./stores/monitor.js";

// 메인 셸 (S-01): 헤더(프로젝트 선택·연결상태) + 좌(채팅방)·중(대화)·우(산출물) 3분할.
// 모든 데이터는 선택 project_id 기준(store). 백엔드 미연결 시 degraded(목업) 배너 표시.
//
// UI-01: 좌/우 패널 경계를 드래그해 폭 조절(resizable splitter). 중앙(대화)은 나머지(flex-1)를
// 채운다. 조절한 폭은 localStorage 에 저장해 새로고침 후에도 유지한다.
const PANEL_W_KEY = "agiteamapp.panelW";
const LEFT_MIN = 220, LEFT_MAX = 560; // 채팅방 패널 폭 한계
const RIGHT_MIN = 300; // 산출물 패널 최소 폭(상한은 동적 — 채팅 최소폭만 확보)
const CHAT_MIN = 320;  // 산출물 확대 시 채팅(중앙) 최소 확보 폭(ChatView min-w 와 일치)
const GUTTER = 56;     // splitter + 컨테이너 패딩 여백 보정
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export default {
  name: "App",
  components: { Icon, ProjectSwitcher, ConversationList, ChatView, ArtifactPanel, ArtifactViewer, TeamView },
  data() {
    // artifactBig: '크게'(UI-02) — 채팅 영역 자리에 큰 산출물 뷰. 우측 트리는 유지(파일 클릭 시 큰 뷰 교체).
    return { leftW: 316, rightW: 400, drag: null, viewMode: "single", artifactBig: false };
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
    // 보기 전환(UI-04): single(3분할) ↔ team(전체 팀원 보기)
    async setView(mode) {
      if (this.viewMode === mode) return;
      this.viewMode = mode;
      if (mode === "team") {
        // 좌측 PM 패널·송신을 위해 PM 방 자동 선택 + 6방 미리보기 로드
        const pm = store.rooms.find((r) => r.isPM);
        if (pm && store.selectedRoomId !== pm.roomId) await selectRoom(pm.roomId);
        loadRoomPreviews();
      }
    },
    // 전체 보기에서 방 카드 클릭 → 단일 방 보기로 진입(상세·페이지네이션)
    onOpenRoom(roomId) {
      this.viewMode = "single";
      selectRoom(roomId);
    },
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
        // 우측 경계: 왼쪽으로 끌면 산출물 패널이 넓어짐(부호 반대).
        // 상한 캡 제거 — 산출물을 화면 대부분까지. 채팅은 CHAT_MIN 확보, 좌패널은 LEFT_MIN 까지 양보.
        const avail = this.viewportW() - CHAT_MIN - GUTTER; // 좌+우가 나눠 쓸 폭
        const newRight = clamp(this.drag.startW - dx, RIGHT_MIN, avail - LEFT_MIN);
        const maxLeft = avail - newRight;
        if (this.leftW > maxLeft) this.leftW = Math.max(LEFT_MIN, maxLeft); // 좌패널 양보
        this.rightW = newRight;
      }
    },
    viewportW() {
      return typeof window !== "undefined" ? window.innerWidth : 1440;
    },
    // 산출물 패널 동적 상한: 좌패널을 LEFT_MIN 까지 양보한다는 가정의 최대값.
    rightMaxW() {
      return Math.max(RIGHT_MIN, this.viewportW() - LEFT_MIN - CHAT_MIN - GUTTER);
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
        if (Number.isFinite(v?.right)) this.rightW = clamp(v.right, RIGHT_MIN, this.rightMaxW());
        // 복원값 합이 화면을 넘으면 좌패널을 양보해 채팅 최소폭 유지
        const avail = this.viewportW() - CHAT_MIN - GUTTER;
        if (this.leftW + this.rightW > avail) this.leftW = Math.max(LEFT_MIN, avail - this.rightW);
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
        <!-- 보기 토글(UI-04): 전체 팀원 보기 ↔ 단일 방 보기 -->
        <div class="inline-flex items-center gap-1 rounded-[11px] border border-line bg-[#F7F7F8] p-[3px]">
          <button
            @click="setView('team')"
            class="h-[30px] rounded-lg px-3 text-[12.5px] font-bold whitespace-nowrap transition-colors"
            :class="viewMode === 'team' ? 'bg-amber text-white shadow-[0_2px_8px_rgba(221,107,31,0.28)]' : 'text-ink-600 hover:text-ink-800'"
          >전체 팀원 보기</button>
          <button
            @click="setView('single')"
            class="h-[30px] rounded-lg px-3 text-[12.5px] font-bold whitespace-nowrap transition-colors"
            :class="viewMode === 'single' ? 'bg-amber text-white shadow-[0_2px_8px_rgba(221,107,31,0.28)]' : 'text-ink-600 hover:text-ink-800'"
          >단일 방 보기</button>
        </div>
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

    <!-- 단일 방 보기(기존 3분할): 좌/우 패널은 드래그로 폭 조절(UI-01). 중앙(대화)은 나머지(flex-1)를
         채우며 최소폭 보장(좁은 화면 말풍선 세로 잘림 방지) + 그 이하 폭에선 가로 스크롤 폴백 -->
    <div v-if="viewMode === 'single'" class="flex min-h-0 flex-1 overflow-x-auto p-[14px]">
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
      <!-- 중: 대화 (또는 '크게' 시 큰 산출물 뷰) -->
      <ChatView v-if="!artifactBig" />
      <ArtifactViewer v-else big class="min-w-0 flex-1" @collapse="artifactBig = false" />
      <!-- 중↔우 splitter -->
      <div
        class="group flex w-[14px] flex-shrink-0 cursor-col-resize items-center justify-center"
        @mousedown.prevent="startDrag('right', $event)"
        title="드래그하여 폭 조절"
      >
        <div class="h-10 w-[3px] rounded-full bg-line transition-colors group-hover:bg-amber"
             :class="drag && drag.side === 'right' ? 'bg-amber' : ''"></div>
      </div>
      <!-- 우: 산출물 (가변폭). 큰 뷰 모드에선 트리만(파일 클릭 → 중앙 큰 뷰 교체) -->
      <div class="min-h-0 flex-shrink-0" :style="{ width: rightW + 'px' }">
        <ArtifactPanel :tree-only="artifactBig" @expand="artifactBig = true" />
      </div>
    </div>

    <!-- 전체 팀원 보기(UI-04): 좌 PM 풀패널+송신, 우 6역할방 그리드(QA top-left) -->
    <TeamView v-else @open-room="onOpenRoom" />
  </div>
</template>
