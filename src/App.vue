<script>
import Icon from "./components/Icon.vue";
import ProjectSwitcher from "./components/ProjectSwitcher.vue";
import ConversationList from "./components/ConversationList.vue";
import ChatView from "./components/ChatView.vue";
import ArtifactPanel from "./components/ArtifactPanel.vue";
import { store, boot, teardown, selectedProject } from "./stores/monitor.js";

// 메인 셸 (S-01): 헤더(프로젝트 선택·연결상태) + 좌(채팅방)·중(대화)·우(산출물) 3분할.
// 모든 데이터는 선택 project_id 기준(store). 백엔드 미연결 시 degraded(목업) 배너 표시.
export default {
  name: "App",
  components: { Icon, ProjectSwitcher, ConversationList, ChatView, ArtifactPanel },
  computed: {
    store: () => store,
    project() {
      return selectedProject();
    },
    pmConnected() {
      return !!this.project && this.project.pmConnected;
    },
  },
  mounted() {
    boot();
  },
  beforeUnmount() {
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
        <span
          v-if="store.degraded"
          class="flex items-center gap-[7px] rounded-[9px] border border-amber-tintbd bg-amber-tint px-[13px] py-[7px] text-[12.5px] font-semibold text-amber-600"
          :title="store.bootError || ''"
        >
          <Icon name="alert" :size="14" />오프라인(목업)
        </span>
        <span
          v-else
          class="flex items-center gap-[7px] rounded-[9px] border px-[13px] py-[7px] text-[13px] font-semibold"
          :class="pmConnected ? 'border-grn-tintbd bg-grn-tint text-grn' : 'border-line bg-line-soft text-ink-500'"
        >
          <span class="h-[7px] w-[7px] rounded-full" :class="pmConnected ? 'bg-grn ring-[3px] ring-grn/20' : 'bg-ink-300'"></span>
          PM {{ pmConnected ? "연결됨" : "끊김" }}
        </span>
      </div>
    </header>

    <!-- 3분할 본문 -->
    <div class="flex min-h-0 flex-1 gap-[14px] p-[14px]">
      <ConversationList />
      <ChatView />
      <ArtifactPanel />
    </div>
  </div>
</template>
