<script>
import Icon from "./Icon.vue";
import { store, selectRoom, selectedRoom } from "../stores/monitor.js";
import { roleLabel, connectionInfo } from "../api/adapters.js";

// 모바일 전용 채팅방 선택 드롭다운(S-02 좌측 목록의 모바일 대체).
// 모바일에선 좌측 사이드 패널을 숨기므로, 상단에서 방을 셀렉트로 전환한다.
// 식별·표시는 (project_id, role)+display_name. 데스크탑에선 렌더되지 않는다(App.vue isMobile 분기).
const DOT = { live: "bg-grn", off: "bg-ink-300", mock: "bg-amber" };

export default {
  name: "RoomSwitcher",
  components: { Icon },
  data() {
    return { open: false };
  },
  computed: {
    store: () => store,
    current() {
      return selectedRoom();
    },
  },
  methods: {
    roleLabel,
    toggle() {
      this.open = !this.open;
    },
    pick(r) {
      this.open = false;
      if (r.roomId !== store.selectedRoomId) selectRoom(r.roomId);
    },
    // 방 연결 상태 점(LIVE=녹색 · 끊김=회색 · MOCK=amber, DS-60 §4.4)
    dotClass(r) {
      const tone = connectionInfo(r.connectionState, r.runtimeState, { mock: store.degraded }).tone;
      return DOT[tone] || DOT.off;
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
      class="flex w-full items-center gap-2.5 rounded-[11px] border border-line bg-white px-3 py-[9px] text-left"
    >
      <span class="flex h-[7px] w-[7px] flex-shrink-0 rounded-full" :class="current ? dotClass(current) : 'bg-ink-300'"></span>
      <div class="min-w-0 flex-1">
        <div class="text-[11px] font-semibold uppercase tracking-wide text-ink-400">채팅방</div>
        <div class="flex min-w-0 items-center gap-1.5">
          <span class="truncate text-[14px] font-bold leading-tight">{{ current ? current.displayName : "선택 없음" }}</span>
          <span v-if="current" class="flex-shrink-0 rounded-[5px] px-1.5 py-px text-[10.5px] font-bold"
                :class="current.isPM ? 'bg-amber-tint text-amber-600' : 'bg-line-soft text-ink-500'">
            {{ roleLabel(current.role) }}
          </span>
        </div>
      </div>
      <span v-if="current && current.unread" class="flex h-[18px] min-w-[18px] flex-shrink-0 items-center justify-center rounded-[9px] bg-amber px-[5px] text-[11px] font-bold text-white">{{ current.unread }}</span>
      <Icon name="chevronDown" :size="16" class="flex-shrink-0 text-ink-500" />
    </button>

    <div
      v-if="open"
      class="absolute left-0 right-0 top-[calc(100%+6px)] z-30 max-h-[60vh] overflow-y-auto rounded-[13px] border border-line bg-white shadow-[0_8px_28px_rgba(0,0,0,0.12)] nice-scroll"
    >
      <div class="border-b border-line-soft px-3.5 py-2.5 text-[12px] font-semibold text-ink-400">
        채팅방 · {{ store.rooms.length }}
      </div>
      <button
        v-for="r in store.rooms"
        :key="r.roomId"
        @click="pick(r)"
        class="flex w-full items-center gap-3 px-3.5 py-2.5 text-left hover:bg-[#F4F4F6]"
        :class="r.roomId === store.selectedRoomId ? 'bg-amber-sel' : ''"
      >
        <span class="h-2 w-2 flex-shrink-0 rounded-full" :class="dotClass(r)"></span>
        <div class="min-w-0 flex-1">
          <div class="flex min-w-0 items-center gap-1.5">
            <span class="truncate text-[14px] font-semibold">{{ r.displayName }}</span>
            <span class="flex-shrink-0 rounded-[5px] px-1.5 py-px text-[10.5px] font-bold"
                  :class="r.isPM ? 'bg-amber-tint text-amber-600' : 'bg-line-soft text-ink-500'">
              {{ roleLabel(r.role) }}
            </span>
          </div>
        </div>
        <span v-if="r.unread" class="flex h-[18px] min-w-[18px] flex-shrink-0 items-center justify-center rounded-[9px] bg-amber px-[5px] text-[11px] font-bold text-white">{{ r.unread }}</span>
        <Icon v-if="r.roomId === store.selectedRoomId" name="check" :size="15" :stroke="2.4" class="flex-shrink-0 text-amber" />
      </button>
      <div v-if="!store.rooms.length" class="px-3.5 py-4 text-[13px] text-ink-400">방이 없습니다.</div>
    </div>
  </div>
</template>
