<script>
import Icon from "./Icon.vue";
import { store, selectedRoom, canCompose, send } from "../stores/monitor.js";
import { roleLabel } from "../api/adapters.js";

// 중앙 대화 뷰 (S-03).
//  - PM 방: 메시지 송수신(입력창 노출). 발신은 PM surface 단일 경로(WG-MSG-02).
//  - 팀원 방: PM↔팀원 대화 '관찰 뷰' — 입력창을 두지 않는다(읽기 전용).
//  - 메시지 본문은 항상 text 보간({{ }})으로 렌더 → XSS 안전(가이드 §5).
//  - 식별/표시는 (project_id, role)+display_name. surface 미표시.
export default {
  name: "ChatView",
  components: { Icon },
  computed: {
    store: () => store,
    room() {
      return selectedRoom();
    },
    canCompose() {
      return canCompose();
    },
    messages() {
      return store.messages;
    },
    draftProxy: {
      get() {
        return store.draft;
      },
      set(v) {
        store.draft = v;
      },
    },
  },
  watch: {
    "store.selectedRoomId"() {
      this.scrollDown();
    },
    "store.messages": { handler() { this.scrollDown(); }, deep: true },
  },
  methods: {
    roleLabel,
    send,
    fmtTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    },
    scrollDown() {
      this.$nextTick(() => {
        const el = this.$refs.thread;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
  },
  mounted() {
    this.scrollDown();
  },
};
</script>

<template>
  <section class="flex min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-line bg-white">
    <!-- 빈 상태 -->
    <div v-if="!room" class="flex flex-1 items-center justify-center text-[14px] text-ink-400">
      좌측에서 채팅방을 선택하세요.
    </div>

    <template v-else>
      <!-- 헤더 -->
      <div class="flex items-start justify-between border-b border-line-soft px-6 py-5">
        <div class="min-w-0">
          <div class="flex items-center gap-2.5">
            <span class="rounded-[7px] px-[9px] py-[3px] text-[12.5px] font-bold"
                  :class="room.isPM ? 'bg-amber-tint text-amber-600' : 'bg-line-soft text-ink-600'">
              {{ roleLabel(room.role) }}
            </span>
            <h1 class="truncate text-[20px] font-bold tracking-[-0.015em]">{{ room.displayName }}</h1>
            <span class="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] font-bold"
                  :class="room.connectionState === 'connected'
                    ? 'border-grn-tintbd bg-grn-tint text-grn-700'
                    : 'border-line bg-line-soft text-ink-500'">
              <span class="h-1.5 w-1.5 rounded-full bg-current"></span>{{ room.connectionState === "connected" ? "연결됨" : "끊김" }}
            </span>
          </div>
          <p class="mt-[7px] text-[13.5px] text-ink-500">
            <template v-if="room.isPM">PM 방 · 메시지를 입력하면 PM에게 전달됩니다</template>
            <template v-else>관찰 뷰 · PM ↔ {{ room.displayName }} 대화 기록 (읽기 전용)</template>
          </p>
        </div>
      </div>

      <!-- 스레드 -->
      <div ref="thread" class="flex flex-1 flex-col gap-[22px] overflow-y-auto bg-white px-7 py-6 nice-scroll">
        <div v-if="store.messagesLoading" class="flex flex-1 items-center justify-center text-[13px] text-ink-400">대화를 불러오는 중…</div>
        <div v-else-if="!messages.length" class="flex flex-1 items-center justify-center text-[13px] text-ink-400">아직 메시지가 없습니다.</div>

        <div
          v-for="m in messages"
          :key="m.messageId"
          :class="['flex max-w-[74%] gap-3', m.out ? 'ml-auto flex-row' : '']"
        >
          <!-- 받은(좌측) 아바타 -->
          <div
            v-if="!m.out"
            class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full text-[12px] font-semibold"
            :class="m.source === 'role_log' ? 'bg-ink-900 text-white' : 'bg-amber text-white'"
          >{{ roleLabel(m.role) }}</div>

          <div :class="['min-w-0', m.out ? 'flex flex-col items-end' : '']">
            <div class="mb-[7px] flex items-center gap-[7px] text-[13px] font-semibold text-ink-700">
              {{ m.out ? (room.isPM ? "나 → PM" : "PM") : room.displayName }}
              <span v-if="m.unmatched" class="rounded-[5px] bg-line-soft px-1.5 py-px text-[10.5px] font-semibold text-ink-500">미매칭</span>
            </div>

            <div
              v-if="!m.out"
              class="whitespace-pre-wrap break-words rounded-2xl rounded-tl-[5px] border border-line bg-white px-[17px] py-[13px] text-[14.5px] leading-[1.62] text-ink-800"
            >{{ m.text }}</div>
            <div
              v-else
              class="whitespace-pre-wrap break-words rounded-2xl rounded-tr-[5px] border px-[17px] py-[13px] text-[14.5px] leading-[1.62]"
              :class="m.failed ? 'border-red-200 bg-red-50 text-red-700' : 'border-amber-tintbd bg-amber-tint text-amber-800'"
            >{{ m.text }}</div>

            <div class="mt-[7px] flex items-center gap-1.5 text-[11.5px] text-ink-300">
              <span>{{ fmtTime(m.occurredAt) }}</span>
              <span v-if="m.pending" class="text-ink-400">전송 중…</span>
              <span v-else-if="m.failed" class="font-semibold text-red-500">전송 실패</span>
              <Icon v-else-if="m.out" name="check" :size="13" :stroke="2.4" class="text-amber" />
            </div>
          </div>

          <!-- 보낸(우측) 아바타 -->
          <div v-if="m.out" class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-ink-900 text-[12px] font-semibold text-white">
            {{ room.isPM ? "나" : "PM" }}
          </div>
        </div>
      </div>

      <!-- 입력창: PM 방에서만 -->
      <div v-if="canCompose" class="flex-shrink-0 border-t border-line-soft bg-white px-5 py-4">
        <div v-if="store.sendError" class="mb-2 flex items-center gap-1.5 text-[12.5px] font-semibold text-red-500">
          <Icon name="alert" :size="14" />{{ store.sendError }}
        </div>
        <div class="flex items-center gap-2.5">
          <input
            v-model="draftProxy"
            @keyup.enter="send"
            :disabled="store.sending"
            class="h-12 flex-1 rounded-[13px] border border-line bg-[#F4F4F6] px-[18px] text-[14.5px] text-ink-900 outline-none placeholder:text-ink-400 focus:border-amber-tintbd focus:bg-white disabled:opacity-60"
            placeholder="PM에게 메시지를 입력하세요…"
          />
          <button
            @click="send"
            :disabled="store.sending || !draftProxy.trim()"
            class="flex h-12 w-12 items-center justify-center rounded-[13px] bg-amber text-white shadow-[0_2px_8px_rgba(221,107,31,0.32)] hover:bg-amber-600 disabled:opacity-50"
          >
            <Icon name="send" :size="20" />
          </button>
        </div>
      </div>

      <!-- 팀원 방: 읽기 전용 안내(입력창 없음) -->
      <div v-else class="flex flex-shrink-0 items-center justify-center gap-2 border-t border-line-soft bg-[#FAFAFB] px-5 py-4 text-[13px] font-medium text-ink-400">
        <Icon name="lock" :size="15" />이 방은 관찰 전용입니다. 메시지 전송은 PM 방에서만 가능합니다.
      </div>
    </template>
  </section>
</template>
