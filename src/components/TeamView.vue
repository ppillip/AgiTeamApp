<script>
import Icon from "./Icon.vue";
import { store, send, loadRoomPreviews } from "../stores/monitor.js";
import { roleLabel, connectionInfo, provenanceInfo } from "../api/adapters.js";
import { renderMessageBody } from "../lib/sanitize.js";

// 전체 팀원 보기 (UI-04, 뮤즈 시안 기준).
//  - 좌: 제우스(PM) 풀높이 패널 + 채팅 입력창(실제 PM 송신, 기존 send 재사용)
//  - 우: 6개 역할 방 3×2 그리드(읽기전용). QA(아르고스)는 top-left 고정(규약)
//  - 각 방: 역할·별칭·LIVE/끊김·provenance(실/목업)·최근 말풍선. 클릭 → 단일 방 보기
//  - 실데이터(store.roomPreviews) + WS/REST 재사용. mock 위장 금지.

// 역할 → 아바타 약어 / 한 줄 설명
const ABBR = { PM: "PM", Architect: "AR", DeveloperBE: "BE", DeveloperFE: "FE", Designer: "DS", QA: "QA", DevOps: "DO" };
const SUBTITLE = {
  PM: "프로젝트 총괄 · 유저 창구",
  Architect: "계약 · 아키텍처 정합",
  DeveloperBE: "API · WebSocket · 저장소",
  DeveloperFE: "Vue 화면 · 상태 표시",
  Designer: "UI 시안 · HTML 퍼블리싱",
  QA: "검증 · 결함 관리",
  DevOps: "cmux · 런처 · 배포",
};
// 전체 팀원 보기 그리드 표시 순서(이 화면 한정 — 사이드바 ROLE_ORDER 와 무관).
//  - QA(아르고스) top-left 고정 규약 유지.
//  - 유저 지시: 아틀라스(DevOps) ↔ 뮤즈(Designer) 자리 교체 → DevOps 를 Designer 앞으로.
const GRID_ORDER = ["QA", "Architect", "DeveloperBE", "DeveloperFE", "DevOps", "Designer"];
const gridRank = (role) => {
  const i = GRID_ORDER.indexOf(role);
  return i === -1 ? 99 : i;
};
// 방 상태 tone → 칩 색
const STATUS_CLASS = {
  live: "border-grn-tintbd bg-grn-tint text-grn-700",
  off: "border-line bg-line-soft text-ink-500",
  mock: "border-amber-tintbd bg-amber-tint text-amber-600",
};

export default {
  name: "TeamView",
  components: { Icon },
  emits: ["open-room"],
  computed: {
    store: () => store,
    pmRoom() {
      return store.rooms.find((r) => r.isPM) || null;
    },
    // 우측 6역할방: GRID_ORDER 기준(QA top-left 고정 + 아틀라스↔뮤즈 교체)
    gridRooms() {
      const others = store.rooms.filter((r) => !r.isPM);
      return others.slice().sort((a, b) => gridRank(a.role) - gridRank(b.role));
    },
    pmMessages() {
      // 좌측 PM 패널은 선택된(=PM) 방의 실시간 스레드를 그대로 사용
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
  data() {
    // 각 대화창 el(roomId/'pm' → element)과 stick-to-bottom 상태(기본 true)
    return { threadEls: {}, stick: {} };
  },
  methods: {
    roleLabel,
    send,
    // 전송 + 입력창 초기화/재포커스(단일방 ChatView 와 동일 UX).
    submit() {
      if (store.sending || !store.draft.trim()) return;
      send();
      this.$nextTick(() => this.resetComposer());
    },
    resetComposer() {
      const el = this.$refs.composer;
      if (!el) return;
      el.style.height = "auto"; // 전송 후 1행 높이로 복귀
      el.focus(); // 커서 복귀 → 연속 입력
    },
    // 줄 수에 따라 높이 자동 확장(최소 1행 ~ 최대 ~5행, 넘으면 내부 스크롤)
    autoGrow() {
      const el = this.$refs.composer;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 120) + "px";
    },
    // Enter=전송, Shift+Enter=줄바꿈. IME 조합 중 Enter 는 한글 확정이므로 오전송 금지.
    onComposerKeydown(e) {
      if (e.isComposing || e.keyCode === 229) return; // IME 조합 중 → 무시
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault(); // 줄바꿈 삽입 막고 전송
        this.submit();
      }
      // Shift+Enter 는 기본 동작(줄바꿈) 유지
    },
    // 대화창 el 등록(함수 ref). 최초 등록 시 stick=true(로드 시 맨 아래로).
    setThreadEl(key, el) {
      if (el) {
        this.threadEls[key] = el;
        if (this.stick[key] === undefined) this.stick[key] = true;
      } else {
        delete this.threadEls[key];
      }
    },
    // 스크롤 시 하단 근처면 stick 유지, 위로 올리면 추적 해제
    onThreadScroll(key, e) {
      const el = e.target;
      this.stick[key] = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    },
    scrollKeyToBottom(key) {
      const el = this.threadEls[key];
      if (el && this.stick[key] !== false) el.scrollTop = el.scrollHeight;
    },
    // 다음 틱에 stick 상태인 모든 대화창을 맨 아래로(7패널 공통)
    scrollAllToBottom() {
      this.$nextTick(() => {
        for (const k in this.threadEls) this.scrollKeyToBottom(k);
      });
    },
    abbr(role) {
      return ABBR[role] || roleLabel(role);
    },
    subtitle(role) {
      return SUBTITLE[role] || "";
    },
    roomStatus(r) {
      // 연결 표식(connection_state 기준): LIVE/끊김/MOCK
      return connectionInfo(r.connectionState, r.runtimeState, { mock: store.degraded });
    },
    statusClass(tone) {
      return STATUS_CLASS[tone] || STATUS_CLASS.off;
    },
    // 데이터 출처 배지(provenance): hook/transcript=실데이터, mock/끊김 명시
    roomSource(r) {
      if (store.degraded || r.isMock) return { label: "MOCK", real: false };
      if (r.provSource) {
        const p = provenanceInfo(r.provSource);
        return { label: p.label || r.provSource.toUpperCase(), real: p.real };
      }
      return r.connectionState === "connected"
        ? { label: "LIVE", real: true }
        : { label: "DISCONNECTED", real: false };
    },
    previewOf(roomId) {
      const list = store.roomPreviews[roomId] || [];
      return list.slice(-3); // 카드엔 최근 3개
    },
    speakerName(m, room) {
      if (m.out) return room.isPM ? "나" : "PM";
      return room.displayName;
    },
    // 단일 방(ChatView)과 동일한 마크다운 렌더러 — 표·코드·헤딩 GFM 렌더(XSS escape 포함)
    renderMessageBody,
    fmtTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    },
    lastAtOf(r) {
      const list = store.roomPreviews[r.roomId];
      const last = list && list.length ? list[list.length - 1] : null;
      return this.fmtTime(last?.occurredAt || r.lastAt);
    },
    openRoom(r) {
      this.$emit("open-room", r.roomId);
    },
    refresh() {
      loadRoomPreviews();
    },
  },
  watch: {
    // 미리보기/PM 메시지 변경 시 stick 상태인 대화창을 하단 추적(새 메시지 자동 따라가기)
    "store.roomPreviews": { handler() { this.scrollAllToBottom(); }, deep: true },
    "store.messages": { handler() { this.scrollAllToBottom(); }, deep: true },
  },
  mounted() {
    // 전체 보기 진입 시 모든 대화창을 최신(맨 아래)으로
    this.$nextTick(() => {
      for (const k in this.threadEls) this.stick[k] = true;
      this.scrollAllToBottom();
    });
  },
};
</script>

<template>
  <section class="flex min-h-0 flex-1 flex-col gap-3 p-[14px]">
    <!-- 팀 보드: 좌 PM / 우 6그리드 (요약 배너 제거 — 헤더 바로 아래 보드) -->
    <div class="grid min-h-0 flex-1 grid-cols-1 gap-3 xl:grid-cols-[minmax(300px,0.88fr)_minmax(0,2.12fr)]">
      <!-- 좌: PM 풀패널 + 입력창 -->
      <article v-if="pmRoom" class="flex min-h-[360px] flex-col overflow-hidden rounded-2xl border border-line bg-white shadow-[0_18px_50px_rgba(26,26,30,0.07)] xl:min-h-0">
        <header class="grid grid-cols-[52px_minmax(0,1fr)_auto] items-center gap-2.5 border-b border-line-soft p-4">
          <div class="grid h-[52px] w-[52px] place-items-center rounded-[15px] bg-amber text-[15px] font-black text-white">PM</div>
          <div class="min-w-0">
            <div class="flex min-w-0 items-center gap-1.5">
              <span class="flex-shrink-0 rounded-[7px] bg-amber-tint px-1.5 py-[3px] text-[11px] font-black text-amber-600">{{ roleLabel(pmRoom.role) }}</span>
              <span class="truncate text-[18px] font-extrabold">{{ pmRoom.displayName }}</span>
            </div>
            <div class="mt-1 truncate text-[12.5px] font-semibold text-ink-500">{{ subtitle(pmRoom.role) }}</div>
          </div>
          <span class="inline-flex items-center gap-1.5 rounded-full border px-2 py-[5px] text-[10.5px] font-black" :class="statusClass(roomStatus(pmRoom).tone)">
            <span class="h-1.5 w-1.5 rounded-full bg-current"></span>{{ roomStatus(pmRoom).label }}
          </span>
        </header>
        <div class="flex items-center justify-between gap-2 px-4 pt-2.5 text-[11.5px] font-bold text-ink-500">
          <span>마지막 응답 {{ lastAtOf(pmRoom) || "—" }}</span>
          <span class="rounded-md border px-1.5 py-0.5 text-[10px] font-black"
                :class="roomSource(pmRoom).real ? 'border-grn-tintbd bg-grn-tint text-grn-700' : 'border-line bg-line-soft text-ink-500'">{{ roomSource(pmRoom).label }}</span>
        </div>
        <!-- PM 스레드(실시간) — 로드 시 하단, 새 메시지 자동 추적 -->
        <div :ref="(el) => setThreadEl('pm', el)" @scroll="onThreadScroll('pm', $event)" class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4 nice-scroll">
          <div v-if="!pmMessages.length" class="flex flex-1 items-center justify-center text-[12.5px] text-ink-400">아직 메시지가 없습니다.</div>
          <div v-for="m in pmMessages.slice(-12)" :key="m.messageId" class="flex min-w-0 max-w-[92%] flex-col gap-1" :class="m.out ? 'self-end items-end' : ''">
            <div class="flex items-center gap-1.5 text-[11.5px] font-bold text-ink-600">{{ speakerName(m, pmRoom) }}<span class="text-[10.5px] font-medium text-ink-400">{{ fmtTime(m.occurredAt) }}</span></div>
            <!-- 단일방과 동일 md 렌더(md-body md-chat). 버블=전체 펼침, 긴 토큰 anywhere 줄바꿈(가로 짤림X). 스크롤은 thread만. -->
            <div class="md-body md-chat max-w-full rounded-2xl border px-3 py-2.5 [overflow-wrap:anywhere] [word-break:keep-all]"
                 :class="m.out ? 'md-chat-out rounded-tr-[5px] border-amber-tintbd bg-amber-tint' : 'rounded-tl-[5px] border-line bg-white'"
                 v-html="renderMessageBody(m.text)"></div>
          </div>
        </div>
        <!-- 입력창: 실제 PM 송신 -->
        <div class="flex flex-shrink-0 items-end gap-2.5 border-t border-line-soft bg-white p-[13px]">
          <textarea ref="composer" v-model="draftProxy" @input="autoGrow" @keydown="onComposerKeydown" rows="1"
                 class="nice-scroll min-h-[42px] max-h-[120px] min-w-0 flex-1 resize-none overflow-y-auto rounded-xl border border-[#e7e7ea] bg-[#F7F7F8] px-3.5 py-[10px] text-[13.5px] font-semibold leading-[1.45] text-ink-900 outline-none placeholder:text-ink-400 focus:bg-white"
                 :placeholder="`${pmRoom.displayName}(PM)에게 메시지를 입력하세요  (Enter 전송 · Shift+Enter 줄바꿈)`"></textarea>
          <button @click="submit" :disabled="store.sending || !draftProxy.trim()"
                  class="grid h-[42px] w-[42px] flex-shrink-0 place-items-center rounded-xl bg-amber text-white shadow-[0_2px_8px_rgba(221,107,31,0.32)] hover:bg-amber-600 disabled:opacity-50">
            <Icon name="send" :size="18" />
          </button>
        </div>
      </article>

      <!-- 우: 6역할방 3×2 그리드(QA top-left). 행 높이를 고정(비-xl 340px / xl 2등분)해 각 카드
           thread 의 flex 높이가 갇히고 독립 스크롤되게 한다(PM 포함 7방 모두 자기 영역 스크롤). -->
      <div class="grid min-h-0 grid-cols-1 gap-3 auto-rows-[340px] sm:grid-cols-2 xl:grid-cols-3 xl:auto-rows-auto xl:grid-rows-2">
        <article v-for="r in gridRooms" :key="r.roomId"
                 class="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-line bg-white shadow-[0_18px_50px_rgba(26,26,30,0.07)]">
          <header class="grid grid-cols-[40px_minmax(0,1fr)_auto] items-center gap-2.5 border-b border-line-soft px-[13px] pb-[11px] pt-[13px]">
            <div class="grid h-10 w-10 place-items-center rounded-xl bg-amber-tint text-[13px] font-black text-amber-600">{{ abbr(r.role) }}</div>
            <div class="min-w-0">
              <div class="flex min-w-0 items-center gap-1.5">
                <span class="max-w-[120px] flex-shrink-0 truncate rounded-[7px] bg-amber-tint px-1.5 py-[3px] text-[11px] font-black text-amber-600">{{ r.role }}</span>
                <span class="truncate text-[15px] font-extrabold">{{ r.displayName }}</span>
              </div>
              <div class="mt-1 truncate text-[11.5px] font-semibold text-ink-500">{{ subtitle(r.role) }}</div>
            </div>
            <span class="inline-flex items-center gap-1.5 rounded-full border px-2 py-[5px] text-[10.5px] font-black" :class="statusClass(roomStatus(r).tone)">
              <span class="h-1.5 w-1.5 rounded-full bg-current"></span>{{ roomStatus(r).label }}
            </span>
          </header>
          <div class="flex items-center justify-between gap-2 px-[13px] pt-2.5 text-[11.5px] font-bold text-ink-500">
            <span>마지막 응답 {{ lastAtOf(r) || "—" }}</span>
            <span class="rounded-md border px-1.5 py-0.5 text-[10px] font-black"
                  :class="roomSource(r).real ? 'border-grn-tintbd bg-grn-tint text-grn-700' : 'border-line bg-line-soft text-ink-500'">{{ roomSource(r).label }}</span>
          </div>
          <!-- 최근 말풍선(읽기전용) — 로드 시 하단, 새 메시지 자동 추적 -->
          <div :ref="(el) => setThreadEl(r.roomId, el)" @scroll="onThreadScroll(r.roomId, $event)" class="flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto px-[13px] pb-3.5 pt-3 nice-scroll">
            <div v-if="!previewOf(r.roomId).length" class="flex flex-1 items-center justify-center text-[12px] text-ink-400">표시할 대화가 없습니다.</div>
            <div v-for="m in previewOf(r.roomId)" :key="m.messageId" class="flex min-w-0 max-w-[86%] flex-col gap-1" :class="m.out ? 'self-end items-end' : ''">
              <div class="flex items-center gap-1.5 text-[11px] font-bold text-ink-600">{{ speakerName(m, r) }}<span class="text-[10px] font-medium text-ink-400">{{ fmtTime(m.occurredAt) }}</span></div>
              <!-- 단일방과 동일 md 렌더(md-body md-chat): 표·코드·헤딩 GFM. 패널 폭 안 줄바꿈(가로 짤림X), 세로 전체 펼침, 스크롤은 thread만. -->
              <div class="md-body md-chat max-w-full rounded-[14px] border px-2.5 py-2 [overflow-wrap:anywhere] [word-break:keep-all]"
                   :class="m.out ? 'md-chat-out rounded-tr-[5px] border-amber-tintbd bg-amber-tint' : 'rounded-tl-[5px] border-line bg-white'"
                   v-html="renderMessageBody(m.text)"></div>
            </div>
          </div>
          <footer class="flex items-center justify-between gap-2 border-t border-line-soft bg-[#FCFCFD] px-[13px] py-2.5">
            <span class="min-w-0 truncate text-[11.5px] font-semibold text-ink-500">{{ r.role === "QA" ? "고정 자리 · 제우스 오른쪽 위" : "읽기 전용 관찰 방" }}</span>
            <button @click="openRoom(r)" class="flex-shrink-0 rounded-lg border border-amber-tintbd bg-white px-2.5 py-1.5 text-[11.5px] font-black text-amber-600 hover:bg-amber-sel">방 열기</button>
          </footer>
        </article>
      </div>
    </div>
  </section>
</template>
