<script>
import Icon from "./Icon.vue";
import { store, selectRoom } from "../stores/monitor.js";
import { roleLabel, connectionInfo } from "../api/adapters.js";
import { cleanMessageText } from "../lib/sanitize.js";

// 상태 tone → 배지/점 색 (LIVE=실연결 녹색 · 끊김=회색 · MOCK=목업 amber)
const STATUS_CLASS = {
  live: { badge: "bg-grn-tint text-grn-700", dot: "bg-grn" },
  off: { badge: "bg-line-soft text-ink-500", dot: "bg-ink-300" },
  mock: { badge: "bg-amber-tint text-amber-600", dot: "bg-amber" },
};

// 마지막 메시지 미리보기: 본문에 ANSI/터미널 chrome 잔재가 섞여 와도
// 방어적으로 strip 하고 한 줄로 줄여 표시한다(목록은 markdown 렌더하지 않음).
function previewText(s) {
  const t = cleanMessageText(s).replace(/\s*\n+\s*/g, " ").trim();
  return t || "—";
}

// 좌측 채팅방 목록 (S-02, WG-CHAT-01).
// 식별·표시는 (project_id, role) + display_name. surface 는 노출하지 않는다.
// 방 클릭 → 중앙 대화 뷰 전환. 팀원 방은 관찰 뷰(읽기전용)로 진입.
export default {
  name: "ConversationList",
  components: { Icon },
  // select: 방을 클릭할 때마다(같은 방 재클릭 포함) 발생 → 부모(App)가 산출물 크게보기(artifactBig)를
  //   해제해 가운데를 그 방 대화뷰로 되돌린다(UI-02). 방 전환 자체는 아래 selectRoom 이 담당.
  emits: ["select"],
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
    previewText,
    pick(room) {
      // 클릭 시 항상 부모에 알림(같은 방 재클릭도 대화뷰 복귀 필요 — artifactBig 해제 트리거).
      this.$emit("select", room.roomId);
      // 방 전환은 다른 방일 때만(같은 방 재로드 방지). 같은 방이면 selectedRoomId 유지 → 대화 그대로.
      if (room.roomId !== store.selectedRoomId) selectRoom(room.roomId);
    },
    // 방 연결 상태 → {label, tone} (DS-60 §4.4). 연결 표식은 connection_state 기준 3종:
    // connected=LIVE · disconnected=끊김 · 전역 degraded 또는 runtime_state=mock=MOCK.
    // (데이터 출처 mock/real 신뢰는 말풍선 provenance 배지가 별도로 담당 — 차원 분리)
    roomStatus(r) {
      return connectionInfo(r.connectionState, r.runtimeState, { mock: store.degraded });
    },
    statusClass(tone, part) {
      return (STATUS_CLASS[tone] || STATUS_CLASS.off)[part];
    },
  },
};
</script>

<template>
  <section class="flex h-full w-full flex-col overflow-hidden rounded-2xl border border-line bg-white">
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
            :class="statusClass(roomStatus(r).tone, 'dot')"
            :title="roomStatus(r).label"
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
          <div class="mt-0.5 truncate text-[13px] text-ink-500">{{ previewText(r.lastText) }}</div>
          <span
            class="mt-[9px] inline-flex items-center gap-[5px] rounded-[7px] px-[9px] py-[3px] text-[11.5px] font-bold tracking-wide"
            :class="statusClass(roomStatus(r).tone, 'badge')"
          >
            <span class="h-1.5 w-1.5 rounded-full bg-current"></span>{{ roomStatus(r).label }}
          </span>
        </div>
      </button>
    </div>
  </section>
</template>
