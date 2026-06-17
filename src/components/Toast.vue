<script>
import { store } from "../stores/monitor.js";

// 짧은 피드백 토스트(WG-ART-07). store.toast({show,text,tone}) 를 구독해 화면 하단 중앙에 표시.
//  - tone: 'ok'(기본) | 'err'. 자동 사라짐은 store.showToast 의 타이머가 담당.
export default {
  name: "Toast",
  computed: {
    store: () => store,
    toast() {
      return store.toast;
    },
  },
};
</script>

<template>
  <transition
    enter-active-class="transition duration-150 ease-out"
    enter-from-class="translate-y-2 opacity-0"
    enter-to-class="translate-y-0 opacity-100"
    leave-active-class="transition duration-150 ease-in"
    leave-from-class="translate-y-0 opacity-100"
    leave-to-class="translate-y-2 opacity-0"
  >
    <div
      v-if="toast.show"
      class="pointer-events-none fixed bottom-6 left-1/2 z-[70] -translate-x-1/2 rounded-[10px] px-4 py-2.5 text-[13px] font-semibold text-white shadow-[0_8px_28px_rgba(0,0,0,0.22)]"
      :class="toast.tone === 'err' ? 'bg-red-600' : 'bg-ink-900'"
    >
      {{ toast.text }}
    </div>
  </transition>
</template>
