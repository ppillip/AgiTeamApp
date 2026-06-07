<script>
import Icon from "./Icon.vue";
import { store, selectRoom } from "../stores/monitor.js";
import { roleLabel } from "../api/adapters.js";

// 좌측 채팅방 목록 (S-02, WG-CHAT-01).
// 식별·표시는 (project_id, role) + display_name. surface 는 노출하지 않는다.
// 방 클릭 → 중앙 대화 뷰 전환. 팀원 방은 관찰 뷰(읽기전용)로 진입.
export default {
  name: "ConversationList",
  components: { Icon },
  computed: {
    store: () => store,
    rooms() {
      return store.rooms;
    },
    connectedCount() {
      return store.rooms.filter((r) => r.connectionState === "connected").length;
    },
  },
  methods: {
    roleLabel,
    pick(room) {
      if (room.roomId !== store.selectedRoomId) selectRoom(room.roomId);
    },
  },
};
</script>

<template>
  <section class="flex w-[316px] flex-shrink-0 flex-col overflow-hidden rounded-2xl border border-line bg-white">
    <div class="flex items-center justify-between px-[18px] pt-[18px]">
      <h2 class="text-[16px] font-bold">채팅방</h2>
      <div class="flex gap-0.5">
        <button class="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-ink-500 hover:bg-[#F4F4F6] hover:text-ink-600"><Icon name="sort" :size="16" /></button>
        <button class="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-ink-500 hover:bg-[#F4F4F6] hover:text-ink-600"><Icon name="search" :size="16" /></button>
      </div>
    </div>
    <div class="px-[18px] pb-3 pt-1.5 text-[12.5px] text-ink-400">
      전체 {{ rooms.length }}개 · 연결 {{ connectedCount }}
    </div>

    <div v-if="store.roomsLoading" class="px-[18px] py-6 text-[13px] text-ink-400">불러오는 중…</div>
    <div v-else-if="!rooms.length" class="px-[18px] py-6 text-[13px] text-ink-400">방이 없습니다.</div>

    <div class="flex flex-col gap-1 overflow-y-auto px-3 pb-3 nice-scroll">
      <button
        v-for="r in rooms"
        :key="r.roomId"
        @click="pick(r)"
        :class="['relative flex items-start gap-3 rounded-[13px] px-3 py-3 text-left transition-colors',
                 r.roomId === store.selectedRoomId ? 'bg-amber-sel shadow-[inset_0_0_0_1px_#F4D9BB]' : 'hover:bg-[#F4F4F6]']"
      >
        <span v-if="r.roomId === store.selectedRoomId" class="absolute bottom-3.5 left-0 top-3.5 w-[3px] rounded bg-amber"></span>
        <div
          class="relative flex h-[38px] w-[38px] flex-shrink-0 items-center justify-center rounded-xl text-[14px] font-semibold"
          :class="r.roomId === store.selectedRoomId ? 'bg-amber text-white' : (r.isPM ? 'bg-amber-tint text-amber-600' : 'bg-line-soft text-ink-600')"
        >
          {{ r.mono }}
          <span
            class="absolute -bottom-0.5 -right-0.5 h-[11px] w-[11px] rounded-full border-2 border-white"
            :class="r.connectionState === 'connected' ? 'bg-grn' : 'bg-ink-300'"
            :title="r.connectionState === 'connected' ? '연결됨' : '끊김'"
          ></span>
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center justify-between gap-2">
            <span class="flex min-w-0 items-center gap-1.5">
              <span class="truncate text-[14px] font-semibold">{{ r.displayName }}</span>
              <span class="flex-shrink-0 rounded-[5px] px-1.5 py-px text-[10.5px] font-bold"
                    :class="r.isPM ? 'bg-amber-tint text-amber-600' : 'bg-line-soft text-ink-500'">
                {{ roleLabel(r.role) }}
              </span>
            </span>
            <span v-if="r.unread" class="flex h-[18px] min-w-[18px] flex-shrink-0 items-center justify-center rounded-[9px] bg-amber px-[5px] text-[11px] font-bold text-white">{{ r.unread }}</span>
          </div>
          <div class="mt-0.5 truncate text-[13px] text-ink-500">{{ r.lastText || "—" }}</div>
          <span
            class="mt-[9px] inline-flex items-center gap-[5px] rounded-[7px] px-[9px] py-[3px] text-[11.5px] font-semibold"
            :class="r.connectionState === 'connected' ? 'bg-grn-tint text-grn-700' : 'bg-line-soft text-ink-500'"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-current"></span>{{ r.connectionState === "connected" ? "연결됨" : "끊김" }}
          </span>
        </div>
      </button>
    </div>
  </section>
</template>
