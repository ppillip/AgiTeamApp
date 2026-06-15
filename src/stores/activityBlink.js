// 에이전트 실시간 상태 — 깜빡 감쇠 규약 (요구사항 15-1, DS-110 §8/§9).
//
// 순수 결정 모듈 + 의존성 주입 타이머 상태머신(artifactChange.js 와 동일 관례).
// import.meta·Vue·전역 타이머에 직접 의존하지 않으므로 node 로 단위검증 가능하다.
//
// 모델(DS-110 §8.2, PM 확정 UX):
//  - 서버는 `active` pulse 만 의미 있는 신호로 보낸다. FE 는 그 신호 1건마다 깜빡을 재시작한다.
//  - pulse 수신 → runtimeActivity='active', activityBlinkKey+1, 1500ms 타이머 리셋.
//  - 1500ms 동안 새 pulse 가 없으면 클라가 스스로 idle 로 자연 정지한다(서버발 idle 미수신 전제).
//  - 서버발 idle 이벤트는 무시한다(되살아나도 무시 — §9 마지막 규칙).
//  - WS 재연결 gap replay 로 받은 과거 pulse(occurred_at 이 now 기준 1500ms+ 오래됨)는 재시작하지 않는다.

// 깜빡이 자연 정지하기까지의 시간(ms). poller 주기(1000ms)보다 길어 연속 출력 중엔 끊기지 않는다.
export const ACTIVITY_BLINK_MS = 1500;

// 안전 파서: ISO 문자열/epoch(ms) → epoch(ms) 또는 NaN.
function toEpoch(t) {
  if (t == null) return NaN;
  if (typeof t === "number") return t;
  const ms = new Date(t).getTime();
  return Number.isFinite(ms) ? ms : NaN;
}

// runtime_activity_changed pulse 1건을 깜빡으로 반영할지 결정(순수).
//
//   payload: { runtime_activity, project_id, role, room_id?, last_active_at? } (DS-110 §7 event.payload)
//   occurredAt: 이벤트 occurred_at(ISO|epoch). ※ 신선도 판정에 쓰지 않는다(아래 참조).
//   ctx: { selectedProjectId, now(epoch ms), blinkMs? }
//
// 반환: { apply: boolean, reason }
//   reason: 'active_pulse'(적용) | 'not_active'(서버발 idle 등) | 'no_payload' | 'other_project'(타 프로젝트)
//
// ⚠️ occurred_at 절대비교 gap replay 가드 제거(실측 2026-06-15):
//   서버(backend/poller) 시계와 브라우저 시계의 clock skew 가 나면 실시간 pulse 의 occurred_at 이
//   브라우저 now 보다 수 분 과거/미래로 찍혀, occurred_at>=1.5초 가드가 실시간 pulse 를 전량 오차단했다
//   (실측: now - occurred_at = 231초 → stale_replay 로 차단 → 깜빡 0). 게다가 현재 backend
//   runtime_activity_service 는 DB write 0·실시간 push only 라 gap replay 자체를 하지 않는다(§8.2 전제 부재).
//   → 신선도 기준은 '수신 시각(now)'으로 일원화한다(blinker.pulse 가 lastActivityPulseAt=now 세움).
//   설령 과거 pulse 가 와도 1.5초 후 자연 정지하므로 폭탄 방어는 그대로 성립한다.
export function planActivityPulse(payload, occurredAt, ctx = {}) {
  if (!payload) return { apply: false, reason: "no_payload" };
  // active 신호만 깜빡을 만든다. 서버발 idle/unknown 은 무시한다.
  if (payload.runtime_activity !== "active") return { apply: false, reason: "not_active" };
  // 프로젝트 스코프 가드(이중 방어): 타 프로젝트 이벤트 무시.
  const sel = ctx.selectedProjectId;
  if (payload.project_id && sel && payload.project_id !== sel) {
    return { apply: false, reason: "other_project" };
  }
  return { apply: true, reason: "active_pulse" };
}

// REST 폴백 degrade 판정(DS-110 §9): rooms 응답 last_active_at 이 now 기준 blinkMs 이내면 '동작중' 유지.
// WS 가 주경로이고, REST 는 hint 만 제공한다. polling 주기가 1s 보다 길면 연속 깜빡은 보장되지 않는다(허용 degrade).
export function isRecentlyActive(lastActiveAt, now, blinkMs = ACTIVITY_BLINK_MS) {
  const ts = toEpoch(lastActiveAt);
  if (!Number.isFinite(ts) || typeof now !== "number") return false;
  const delta = now - ts;
  // 미래 시각 방어(불가침): 서버 시계가 앞서면 last_active_at 이 now 보다 미래라 delta<0 →
  // 'now-ts<blinkMs' 가 영구 참이 되어 멈추지 않는다(유저 실측 2026-06-15: 발사 멈춰도 계속 깜빡).
  // 정상 범위 [0, blinkMs) 만 신선으로 인정한다.
  return delta >= 0 && delta < blinkMs;
}

// 카드 활동 인디케이터 라벨 결정(순수, DS-110 §8.3).
//   room: { runtimeActivity, lastActiveAt } · ctx: { degraded, now(epoch ms), blinkMs? }
//   반환: active → { active:true } · idle → { active:false } · 그 외 null. (라벨 없음)
//   ⚠️ 활동 텍스트 라벨('동작중'·'조용함') 표기 폐지(유저 최종확정 2026-06-15): 배지 텍스트는 'LIVE' 단독,
//     활동 신호는 '깜빡이는 점'으로만 표현한다. active 여부만 반환해 점 깜빡(class)을 구동한다.
//
// ⚠️ connection 게이트 없음(PM 긴급 2026-06-15): connectionState 로 깜빡을 막지 않는다.
//   폴러의 runtime_activity=active(WS pulse)는 '지금 실제 출력이 있었다'는 직접 관측이므로,
//   연결 디스커버리 상태(cmux pane 발견)와 독립적으로 깜빡해야 한다. 죽은 팀은 pulse 가 안 와
//   1.5초 뒤 자연 정지하므로 오작동 없음. 단 degraded(mock)만 제외한다(가짜 깜빡 금지).
export function cardActivityState(room, ctx = {}) {
  if (!room || ctx.degraded) return null;
  // 반응성 heartbeat 는 store.nowTick(ctx.now) 의존으로 유지(컴포넌트가 매초 재평가 → 1.5초 자가정지).
  // 단 WS pulse 는 blinker.pulse 가 Date.now() 로 찍는 즉시값이고 store.nowTick 은 최대 1초 늦을 수 있어,
  // lastActivityPulseAt 이 ctx.now 보다 살짝 미래면 WS 판정에 한해 now 를 pulse 시각까지 끌어올린다.
  // REST lastActiveAt 은 서버 시각이므로 이 보정을 적용하지 않는다(미래 시각 영구 깜빡 방어 유지).
  const now = typeof ctx.now === "number" ? ctx.now : Date.now();
  const wsTs = toEpoch(room.lastActivityPulseAt);
  const wsNow = Number.isFinite(wsTs) ? Math.max(now, wsTs) : now;
  const blinkMs = ctx.blinkMs ?? ACTIVITY_BLINK_MS;
  // '동작중' 판정은 시각 기반(DS-110 §8.2/§9):
  //   - lastActivityPulseAt: WS pulse 가 blinker.pulse 로 세운 시각(주경로). 신선하면 깜빡 중.
  //   - lastActiveAt: REST degrade hint. now-1.5초 이내만 유지(§9).
  // ⚠️ REST runtime_activity='active' 필드는 단독 신뢰 금지: backend 가 idle 을 발행하지 않아(§3.2)
  //   registry 가 마지막 active 로 굳어 stale('동작 멈췄는데 active')일 수 있다. 시각으로만 판정한다.
  if (
    isRecentlyActive(room.lastActivityPulseAt, wsNow, blinkMs) ||
    isRecentlyActive(room.lastActiveAt, now, blinkMs)
  ) {
    return { active: true };
  }
  if (room.runtimeActivity === "idle") return { active: false }; // idle: 라벨 없음(유저 요청 2026-06-15) — 점 색 구분만 유지, 'LIVE'만 표기
  return null; // 신선한 pulse 없음 → 표식 없음(자연 정지)
}

// 깜빡 감쇠 타이머 상태머신(의존성 주입). room 객체를 직접 mutate 한다.
//   opts: { blinkMs, now(()=>epoch), setTimer((fn,ms)=>id), clearTimer((id)=>void) }
//   기본값은 브라우저 전역(Date.now/setTimeout/clearTimeout). 테스트는 fake clock 을 주입한다.
//
// 반환 API:
//   pulse(room, occurredAt?) — room 을 active 로 만들고 blinkMs 후 idle 로 전환(타이머 리셋).
//   cancel(key)              — 특정 room(roomId) 타이머 취소.
//   cancelAll()              — 전체 타이머 취소(프로젝트 전환/정상 종료).
//   pending()                — 진행 중 타이머 수(테스트/진단).
export function createActivityBlinker(opts = {}) {
  const blinkMs = opts.blinkMs ?? ACTIVITY_BLINK_MS;
  const now = opts.now || (() => Date.now());
  const setTimer =
    opts.setTimer || ((fn, ms) => setTimeout(fn, ms));
  const clearTimer =
    opts.clearTimer || ((id) => clearTimeout(id));

  const timers = new Map(); // key(roomId) -> timerId

  function pulse(room, _occurredAt) {
    if (!room) return;
    // 표시·재시작 상태: pulse '수신 시각'(now) 기록 + 애니메이션 재시작 key 증가.
    // ⚠️ occurred_at(서버 시계) 을 쓰지 않는다(_occurredAt 무시): clock skew 로 과거/미래로 찍히면
    //   cardActivityState 의 isRecentlyActive 가 실시간 pulse 를 신선하지 않다고 오판한다. 실시간
    //   WS push 는 '받은 순간'이 곧 신선함이므로 now() 로 고정한다(실측 차단점 수정 2026-06-15).
    room.lastActivityPulseAt = now();
    room.activityBlinkKey = (room.activityBlinkKey || 0) + 1;
    room.runtimeActivity = "active";
    // 타이머 0 리셋: 이전 idle 예약을 취소하고 새로 건다 → 연속 pulse 중 끊김 없음.
    const key = room.roomId;
    const prev = timers.get(key);
    if (prev != null) clearTimer(prev);
    const id = setTimer(() => {
      room.runtimeActivity = "idle"; // blinkMs 무신호 → 클라 자가 idle 판정
      timers.delete(key);
    }, blinkMs);
    timers.set(key, id);
  }

  function cancel(key) {
    const prev = timers.get(key);
    if (prev != null) {
      clearTimer(prev);
      timers.delete(key);
    }
  }

  function cancelAll() {
    for (const id of timers.values()) clearTimer(id);
    timers.clear();
  }

  function pending() {
    return timers.size;
  }

  return { pulse, cancel, cancelAll, pending };
}
