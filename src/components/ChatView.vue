<script>
import Icon from "./Icon.vue";
import {
  store,
  selectedRoom,
  canCompose,
  send,
  loadOlderMessages,
  addComposerImages,
  removeComposerAttachment,
  canSend,
} from "../stores/monitor.js";
import { roleLabel, connectionInfo } from "../api/adapters.js";
import { cardActivityState } from "../stores/activityBlink.js";
import { attachmentPreviewSrc } from "../api/index.js";
import { renderMessageBody } from "../lib/sanitize.js";

// provenance tone → 배지 색(DS-60 §6.1). live=실데이터(녹색), sent=발신(amber),
// manual=수동(파랑), mock=목업(회색), diag=진단(어두운 회색).
const PROV_CLASS = {
  live: "border-grn-tintbd bg-grn-tint text-grn-700",
  sent: "border-amber-tintbd bg-amber-tint text-amber-700",
  manual: "border-blue-200 bg-blue-50 text-blue-600",
  mock: "border-line bg-line-soft text-ink-500",
  diag: "border-ink-200 bg-ink-900/5 text-ink-500",
  unknown: "border-line bg-line-soft text-ink-400",
};

// 중앙 대화 뷰 (S-03).
//  - PM 방: 메시지 송수신(입력창 노출). 발신은 PM surface 단일 경로(WG-MSG-02).
//  - 팀원 방: PM↔팀원 대화 '관찰 뷰' — 입력창을 두지 않는다(읽기 전용).
//  - 메시지 본문은 항상 text 보간({{ }})으로 렌더 → XSS 안전(가이드 §5).
//  - 식별/표시는 (project_id, role)+display_name. surface 미표시.
export default {
  name: "ChatView",
  components: { Icon },
  // mobile(UI-06): 모바일 레이아웃에서 true. 말풍선 max-width 를 거의 풀폭으로 넓히고
  // thread 좌우 패딩을 줄여 좁은 화면 가용폭을 최대한 쓴다(데스크탑은 prop 미전달=false → 무변경).
  props: { mobile: { type: Boolean, default: false } },
  data() {
    return { prevScrollHeight: null, dragOver: false };
  },
  computed: {
    store: () => store,
    room() {
      return selectedRoom();
    },
    canCompose() {
      return canCompose();
    },
    composerAttachments() {
      return store.composerAttachments;
    },
    canSendNow() {
      return canSend();
    },
    messages() {
      return store.messages;
    },
    // 메시지 + 세션 구분선(team_session_id 변화 지점, DS-60 §3). 첫 세션은 구분선 생략.
    threadItems() {
      const items = [];
      let prevSession;
      for (const m of store.messages) {
        const sid = m.teamSessionId || null;
        if (sid && prevSession !== undefined && sid !== prevSession) {
          items.push({ kind: "divider", id: "sess_" + m.messageId, sessionId: sid });
        }
        if (sid != null) prevSession = sid;
        else if (prevSession === undefined) prevSession = null;
        items.push({ kind: "msg", id: m.messageId, m });
      }
      return items;
    },
    draftProxy: {
      get() {
        return store.draft;
      },
      set(v) {
        store.draft = v;
      },
    },
    // 방 연결 상태 3종(DS-60 §4.4): LIVE/끊김/MOCK
    connStatus() {
      const r = this.room;
      if (!r) return { label: "끊김", tone: "off" };
      // 연결 표식은 connection_state 기준(데이터 출처는 말풍선 provenance 배지가 담당)
      return connectionInfo(r.connectionState, r.runtimeState, { mock: store.degraded });
    },
    // 런타임 활동 2차 인디케이터(요구사항 15-1, DS-110 §8.3 단일방 헤더). 전체보기(TeamView)와 동일 판정.
    //   this.room 은 selectedRoom()=store.rooms 의 같은 reactive 객체 → applyRuntimeActivity 의 갱신이 그대로 반영.
    activity() {
      return cardActivityState(this.room, { degraded: store.degraded, now: store.nowTick });
    },
    // 수집기 상태 경고 (DS-60 §4.2·§10.2: collector delayed/stopped → FE 표시).
    collectorWarn() {
      const r = this.room;
      if (!r) return null;
      if (r.collectorState === "delayed") return { text: "수집 지연", tone: "warn" };
      if (r.collectorState === "stopped") return { text: "수집 중단", tone: "err" };
      return null;
    },
  },
  watch: {
    "store.selectedRoomId"() {
      this.prevScrollHeight = null;
      this.scrollDown();
    },
    "store.messages": {
      handler() {
        this.$nextTick(() => {
          const el = this.$refs.thread;
          if (!el) return;
          if (this.prevScrollHeight != null) {
            // 과거 prepend(더보기) → 기존 위치 유지(맨 아래로 튀지 않게)
            el.scrollTop = el.scrollHeight - this.prevScrollHeight;
            this.prevScrollHeight = null;
          } else {
            el.scrollTop = el.scrollHeight; // 신규 하단 추가 → 맨 아래로
          }
        });
      },
      deep: true,
    },
  },
  methods: {
    roleLabel,
    send,
    renderMessageBody,
    loadOlderMessages,
    removeComposerAttachment,
    // 전송 + 입력창 초기화/재포커스(연속 입력 끊김 방지).
    // 텍스트가 없어도 준비된 이미지 첨부가 있으면 전송 가능(canSend).
    submit() {
      if (!canSend()) return;
      send();
      this.$nextTick(() => this.resetComposer());
    },
    // 말풍선/썸네일 src: 서버 preview_url 우선(self-contained), 없으면 로컬 blob(localUrl).
    attThumbSrc(att) {
      return attachmentPreviewSrc(att.previewUrl) || att.localUrl || null;
    },
    // 입력창 pending 썸네일 src: 업로드 중엔 preview_url 이 없으므로 로컬 blob 우선.
    pendingThumbSrc(att) {
      return att.localUrl || attachmentPreviewSrc(att.previewUrl) || null;
    },
    // 클립보드 paste 에서 이미지 blob 추출(PNG/JPG/WebP/GIF). 이미지가 있으면 기본 붙여넣기 막음.
    onComposerPaste(e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      const files = [];
      for (const it of items) {
        if (it.kind === "file" && it.type && it.type.indexOf("image/") === 0) {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        e.preventDefault(); // 이미지 데이터 URL 이 텍스트로 끼어드는 것 방지
        addComposerImages(files);
      }
    },
    // 파일 선택(클립 버튼)
    onPickFiles(e) {
      const files = e.target.files;
      if (files && files.length) addComposerImages(files);
      e.target.value = ""; // 같은 파일 재선택 허용
    },
    openFilePicker() {
      const el = this.$refs.fileInput;
      if (el) el.click();
    },
    // 드래그&드롭 업로드
    onDrop(e) {
      this.dragOver = false;
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {
        const imgs = Array.from(files).filter((f) => f.type && f.type.indexOf("image/") === 0);
        if (imgs.length) addComposerImages(imgs);
      }
    },
    onDragOver() {
      if (this.canCompose) this.dragOver = true;
    },
    onDragLeave() {
      this.dragOver = false;
    },
    resetComposer() {
      const el = this.$refs.composer;
      if (!el) return;
      el.style.height = "auto"; // 전송 후 1행 높이로 복귀
      el.focus(); // 커서 복귀 → 연속 입력
    },
    // 줄 수에 따라 높이 자동 확장(최소 1행 ~ 최대 ~6행, 넘으면 내부 스크롤)
    autoGrow() {
      const el = this.$refs.composer;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 148) + "px";
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
    // 메시지 시각 포맷. sec=true 면 초까지(HH:MM:SS) — 말풍선 시각(전송 타이밍 체감용, 유저요청 2026-06-16).
    fmtTime(iso, sec = false) {
      if (!iso) return "";
      const d = new Date(iso);
      if (isNaN(d)) return "";
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      if (!sec) return `${hh}:${mm}`;
      return `${hh}:${mm}:${String(d.getSeconds()).padStart(2, "0")}`;
    },
    scrollDown() {
      this.$nextTick(() => {
        const el = this.$refs.thread;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
    // 위로 스크롤하면 더 과거 20개 로드(before-cursor). 스크롤 위치는 watch 에서 보존.
    onThreadScroll() {
      const el = this.$refs.thread;
      if (!el) return;
      if (el.scrollTop < 60 && store.messagesHasMore && !store.loadingOlder) {
        this.prevScrollHeight = el.scrollHeight;
        loadOlderMessages();
      }
    },
    loadOlderClick() {
      const el = this.$refs.thread;
      if (el) this.prevScrollHeight = el.scrollHeight;
      loadOlderMessages();
    },
    // 말풍선 출처 배지: degraded(전역 mock) 우선, 아니면 메시지 provenance.
    badgeFor(m) {
      if (store.degraded) return { label: "MOCK", tone: "mock" };
      if (m.provLabel) return { label: m.provLabel, tone: m.provTone };
      return null;
    },
    provClass(tone) {
      return PROV_CLASS[tone] || PROV_CLASS.unknown;
    },
  },
  mounted() {
    this.scrollDown();
  },
};
</script>

<template>
  <section class="flex min-w-[320px] flex-1 flex-col overflow-hidden rounded-2xl border border-line bg-white">
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
            <span class="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] font-bold tracking-wide"
                  :class="connStatus.tone === 'live'
                    ? 'border-grn-tintbd bg-grn-tint text-grn-700'
                    : connStatus.tone === 'mock'
                    ? 'border-amber-tintbd bg-amber-tint text-amber-600'
                    : 'border-line bg-line-soft text-ink-500'">
              <span class="h-1.5 w-1.5 rounded-full" :key="'blink-' + (room.activityBlinkKey || 0)" :class="activity ? (activity.active ? 'bg-current animate-activity-blink' : 'bg-grn/50') : 'bg-current'"></span>{{ connStatus.label }}<template v-if="activity?.active"> · 동작중</template>
            </span>
            <!-- 수집기 상태 경고 (DS-60: collector delayed/stopped) -->
            <span v-if="collectorWarn"
                  class="inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] font-bold"
                  :class="collectorWarn.tone === 'err'
                    ? 'border-red-200 bg-red-50 text-red-600'
                    : 'border-amber-tintbd bg-amber-tint text-amber-700'"
                  title="수집기(transcript/hook) 상태. 본문 갱신이 지연되거나 중단된 상태입니다.">
              <Icon name="alert" :size="12" />{{ collectorWarn.text }}
            </span>
          </div>
          <p class="mt-[7px] text-[13.5px] text-ink-500">
            <template v-if="room.isPM">PM 방 · 메시지를 입력하면 PM에게 전달됩니다</template>
            <template v-else>관찰 뷰 · PM ↔ {{ room.displayName }} 대화 기록 (읽기 전용)</template>
          </p>
        </div>
      </div>

      <!-- 스레드 -->
      <div ref="thread" @scroll="onThreadScroll" class="flex flex-1 flex-col overflow-y-auto bg-white nice-scroll" :class="mobile ? 'gap-4 px-3 py-4' : 'gap-[22px] px-7 py-6'">
        <div v-if="store.messagesLoading" class="flex flex-1 items-center justify-center text-[13px] text-ink-400">대화를 불러오는 중…</div>
        <div v-else-if="!messages.length" class="flex flex-1 items-center justify-center text-[13px] text-ink-400">아직 메시지가 없습니다.</div>

        <!-- 더보기(위로 스크롤) — 더 과거 20개 (WG-CHAT-02 before-cursor) -->
        <div v-if="messages.length && (store.messagesHasMore || store.loadingOlder)" class="flex justify-center pb-1">
          <span v-if="store.loadingOlder" class="text-[12px] text-ink-400">이전 대화 불러오는 중…</span>
          <button v-else @click="loadOlderClick"
                  class="rounded-full border border-line bg-[#F4F4F6] px-3.5 py-1 text-[12px] font-semibold text-ink-500 hover:bg-line-soft">
            ↑ 이전 대화 더보기
          </button>
        </div>

        <template v-for="it in threadItems" :key="it.id">
          <!-- 세션 구분선 (team_session_id 변화 지점) -->
          <div v-if="it.kind === 'divider'" class="flex items-center gap-3 py-1 text-[11px] font-semibold text-ink-400">
            <span class="h-px flex-1 bg-line"></span>
            <span class="rounded-full border border-line bg-[#FAFAFB] px-2.5 py-0.5">새 세션 · {{ it.sessionId }}</span>
            <span class="h-px flex-1 bg-line"></span>
          </div>

          <!-- 메시지 -->
          <div v-else :class="['flex gap-3', mobile ? 'max-w-[96%]' : 'max-w-[74%]', it.m.out ? 'ml-auto flex-row' : '']">
            <!-- 받은(좌측) 아바타 -->
            <div
              v-if="!it.m.out"
              class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full text-[12px] font-semibold"
              :class="it.m.diagnostic ? 'bg-ink-900 text-white' : 'bg-amber text-white'"
            >{{ roleLabel(it.m.role) }}</div>

            <div :class="['min-w-0', it.m.out ? 'flex flex-col items-end' : '']">
              <div class="mb-[7px] flex items-center gap-[7px] text-[13px] font-semibold text-ink-700">
                {{ it.m.out ? (room.isPM ? "나 → PM" : "PM") : room.displayName }}
                <!-- 출처(provenance) 배지: 실(LIVE)/발신(SENT)/수동(MANUAL)/목업(MOCK)/진단(DIAGNOSTIC) -->
                <span v-if="badgeFor(it.m)" class="rounded-[5px] border px-1.5 py-px text-[10px] font-bold tracking-wide"
                      :class="provClass(badgeFor(it.m).tone)"
                      title="출처(DS-60 §6.1): hook/transcript=실데이터 · webgui/bridge=발신 · manual=수동 · mock=목업 · read_screen=진단">
                  {{ badgeFor(it.m).label }}
                </span>
                <span v-if="it.m.unmatched" class="rounded-[5px] bg-line-soft px-1.5 py-px text-[10.5px] font-semibold text-ink-500" title="발신과 매칭되지 않은 수신 메시지(DS-60 §6.8)">미매칭</span>
              </div>

              <!-- 본문: 방어적 정제(ANSI/터미널 chrome strip) 후 마크다운 렌더. renderMessageBody 가 전체 escape → XSS 안전 -->
              <div
                v-if="!it.m.out"
                class="md-body md-chat break-words rounded-2xl rounded-tl-[5px] border bg-white px-[17px] py-[13px]"
                :class="it.m.degraded ? 'border-amber-tintbd' : 'border-line'"
                v-html="renderMessageBody(it.m.text)"
              ></div>
              <div
                v-else-if="it.m.text"
                class="md-body md-chat break-words rounded-2xl rounded-tr-[5px] border px-[17px] py-[13px]"
                :class="it.m.failed ? 'md-chat-fail border-red-200 bg-red-50' : 'md-chat-out border-amber-tintbd bg-amber-tint'"
                v-html="renderMessageBody(it.m.text)"
              ></div>

              <!-- 이미지 첨부 썸네일(DV-91): 순서 보존. 클릭 시 원본(preview) 새 탭. -->
              <div v-if="it.m.attachments && it.m.attachments.length"
                   class="mt-2 flex flex-wrap gap-2"
                   :class="it.m.out ? 'justify-end' : ''">
                <a v-for="(att, ai) in it.m.attachments" :key="att.attachmentId || ai"
                   :href="attThumbSrc(att)" target="_blank" rel="noopener noreferrer"
                   class="block overflow-hidden rounded-xl border border-line bg-[#FAFAFB]">
                  <img :src="attThumbSrc(att)" :alt="att.filename || att.name || 'image'"
                       class="max-h-[180px] max-w-[220px] object-contain" />
                </a>
              </div>

              <div class="mt-[7px] flex items-center gap-1.5 text-[11.5px] text-ink-300">
                <span>{{ fmtTime(it.m.occurredAt, true) }}</span>
                <span v-if="it.m.pending" class="text-ink-400">전송 중…</span>
                <span v-else-if="it.m.failed" class="font-semibold text-red-500">전송 실패</span>
                <Icon v-else-if="it.m.out" name="check" :size="13" :stroke="2.4" class="text-amber" />
              </div>
            </div>

            <!-- 보낸(우측) 아바타 -->
            <div v-if="it.m.out" class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-ink-900 text-[12px] font-semibold text-white">
              {{ room.isPM ? "나" : "PM" }}
            </div>
          </div>
        </template>
      </div>

      <!-- 입력창: PM 방에서만 -->
      <div
        v-if="canCompose"
        class="relative flex-shrink-0 border-t border-line-soft bg-white px-5 py-4"
        :class="dragOver ? 'ring-2 ring-amber-tintbd ring-inset' : ''"
        @drop.prevent="onDrop"
        @dragover.prevent="onDragOver"
        @dragleave="onDragLeave"
      >
        <!-- 드래그 오버레이 안내 -->
        <div v-if="dragOver" class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-amber-tint/70 text-[13px] font-semibold text-amber-700">
          이미지를 여기에 놓아 첨부
        </div>

        <div v-if="store.sendError" class="mb-2 flex items-center gap-1.5 text-[12.5px] font-semibold text-red-500">
          <Icon name="alert" :size="14" />{{ store.sendError }}
        </div>

        <!-- pending 첨부 썸네일 스트립(전송 전 제거 가능) -->
        <div v-if="composerAttachments.length" class="mb-2.5 flex flex-wrap gap-2">
          <div v-for="att in composerAttachments" :key="att.clientId"
               class="relative h-16 w-16 overflow-hidden rounded-lg border"
               :class="att.status === 'error' ? 'border-red-300' : 'border-line'">
            <img v-if="pendingThumbSrc(att)" :src="pendingThumbSrc(att)" :alt="att.name"
                 class="h-full w-full object-cover" :class="att.status !== 'ready' ? 'opacity-60' : ''" />
            <div v-else class="flex h-full w-full items-center justify-center bg-line-soft text-ink-400">
              <Icon name="alert" :size="16" />
            </div>
            <!-- 업로드 진행/상태 오버레이 -->
            <div v-if="att.status === 'uploading'" class="absolute inset-0 flex items-center justify-center bg-black/30 text-[10.5px] font-bold text-white">
              {{ att.progress }}%
            </div>
            <div v-else-if="att.status === 'error'" class="absolute inset-0 flex items-center justify-center bg-red-500/25 text-red-700"
                 :title="att.error || '업로드 실패'">
              <Icon name="alert" :size="16" />
            </div>
            <!-- 제거 버튼 -->
            <button @click="removeComposerAttachment(att.clientId)" type="button"
                    class="absolute right-0.5 top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-ink-900/70 text-white hover:bg-ink-900"
                    title="첨부 제거">
              <Icon name="x" :size="10" :stroke="3" />
            </button>
          </div>
        </div>

        <input ref="fileInput" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple class="hidden" @change="onPickFiles" />

        <div class="flex items-end gap-2.5">
          <!-- 이미지 첨부 버튼 -->
          <button
            @click="openFilePicker"
            type="button"
            class="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-[13px] border border-line bg-[#F4F4F6] text-ink-500 hover:bg-line-soft"
            title="이미지 첨부 (붙여넣기·드래그도 가능)"
          >
            <Icon name="paperclip" :size="19" />
          </button>
          <textarea
            ref="composer"
            v-model="draftProxy"
            @input="autoGrow"
            @keydown="onComposerKeydown"
            @paste="onComposerPaste"
            rows="1"
            class="nice-scroll min-h-[48px] max-h-[148px] flex-1 resize-none overflow-y-auto rounded-[13px] border border-line bg-[#F4F4F6] px-[18px] py-[13px] text-[14.5px] leading-[1.45] text-ink-900 outline-none placeholder:text-ink-400 focus:border-amber-tintbd focus:bg-white"
            placeholder="PM에게 메시지를 입력하세요…  (Enter 전송 · Shift+Enter 줄바꿈 · 이미지 붙여넣기 가능)"
          ></textarea>
          <button
            @click="submit"
            :disabled="!canSendNow"
            class="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-[13px] bg-amber text-white shadow-[0_2px_8px_rgba(221,107,31,0.32)] hover:bg-amber-600 disabled:opacity-50"
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
