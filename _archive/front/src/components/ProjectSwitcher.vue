<script>
import Icon from "./Icon.vue";
import { store, selectProject, selectedProject } from "../stores/monitor.js";

// 헤더 프로젝트 선택/전환 (S-01, WG-PROJ-01).
// 여러 AgiTeam 중 하나 선택 → 선택 project_id 기준으로 방·대화·산출물 재로드(store 가 처리).
export default {
  name: "ProjectSwitcher",
  components: { Icon },
  data() {
    return { open: false };
  },
  computed: {
    store: () => store,
    current() {
      return selectedProject();
    },
  },
  methods: {
    toggle() {
      this.open = !this.open;
    },
    pick(p) {
      this.open = false;
      if (p.projectId !== store.selectedProjectId) selectProject(p.projectId);
    },
    onClickOutside(e) {
      if (!this.$el.contains(e.target)) this.open = false;
    },
  },
  mounted() {
    document.addEventListener("click", this.onClickOutside);
  },
  beforeUnmount() {
    document.removeEventListener("click", this.onClickOutside);
  },
};
</script>

<template>
  <div class="relative">
    <button
      @click.stop="toggle"
      class="flex items-center gap-2.5 rounded-[11px] border border-line bg-white px-3 py-[7px] text-left hover:bg-[#F4F4F6]"
    >
      <span
        class="h-[7px] w-[7px] flex-shrink-0 rounded-full"
        :class="current && current.connected ? 'bg-grn ring-[3px] ring-grn/20' : 'bg-ink-300'"
      ></span>
      <div class="min-w-0">
        <div class="text-[11px] font-semibold uppercase tracking-wide text-ink-400">프로젝트</div>
        <div class="truncate text-[14px] font-bold leading-tight">
          {{ current ? current.title : "선택 없음" }}
        </div>
      </div>
      <Icon name="chevronDown" :size="16" class="text-ink-500" />
    </button>

    <div
      v-if="open"
      class="absolute left-0 top-[calc(100%+6px)] z-30 w-[280px] overflow-hidden rounded-[13px] border border-line bg-white shadow-[0_8px_28px_rgba(0,0,0,0.12)]"
    >
      <div class="border-b border-line-soft px-3.5 py-2.5 text-[12px] font-semibold text-ink-400">
        떠 있는 AgiTeam · {{ store.projects.length }}
      </div>
      <button
        v-for="p in store.projects"
        :key="p.projectId"
        @click="pick(p)"
        class="flex w-full items-center gap-3 px-3.5 py-2.5 text-left hover:bg-[#F4F4F6]"
        :class="p.projectId === store.selectedProjectId ? 'bg-amber-sel' : ''"
      >
        <span
          class="h-2 w-2 flex-shrink-0 rounded-full"
          :class="p.connected ? 'bg-grn' : 'bg-ink-300'"
        ></span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[14px] font-semibold">{{ p.title }}</div>
          <div class="text-[12px] text-ink-400">
            방 {{ p.roomCount }} · PM {{ p.pmConnected ? "연결됨" : "끊김" }}
          </div>
        </div>
        <Icon
          v-if="p.projectId === store.selectedProjectId"
          name="check"
          :size="15"
          :stroke="2.4"
          class="text-amber"
        />
      </button>
      <div v-if="!store.projects.length" class="px-3.5 py-4 text-[13px] text-ink-400">
        발견된 프로젝트가 없습니다.
      </div>
    </div>
  </div>
</template>
