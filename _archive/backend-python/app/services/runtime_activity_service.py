"""요구사항 15-1 read-screen poller active pulse 수신 서비스 (DS-110 §6 / §7).

[2026-06-15 설계정정 — DB 경로 전면 제거 / 유저 지시]
liveness 깜빡은 1.5초면 사라지는 **휘발성 신호**다. 폴러가 1초마다 보내는 pulse 를 영구
``webgui_runtime_event`` 로 적재하면 테이블이 폭발하고, source CHECK 제약 위반으로 503 까지
유발했다. 그래서 본 경로는 **DB 를 일절 건드리지 않는다**:

- room upsert 안 함 (liveness 가 room 을 만들지 않는다 — room 은 hook/대화수집이 만든다).
- runtime_event INSERT 안 함 (영구 저장 0).
- 처리: 인메모리 ``activity_registry`` pulse 갱신 + dedup(같은 hash skip) + WS 즉시 fanout.

(B)폴러 모델: poller 가 1초 read-screen diff 로 감지한 `active` pulse 만 받는다. 서버는 idle 을
발행하지 않는다(§3.2/§6.2.7) — idle 은 FE 가 1.5초 무신호로 자가 판정한다. raw screen 원문은
전송하지 않는다 — normalized snapshot 의 hash/byte 길이만(§10.1).

FE 매칭(monitor.js)은 ``room_id`` 또는 ``(project_id, role)`` 둘 중 하나로 카드를 찾으므로,
DB room_id 없이 (project_id, role) 만으로 TeamView 깜빡이 정확히 동작한다.
"""
from __future__ import annotations

import uuid

from .. import errors
from .events import hub
from .log_collector import (
    ACTIVITY_ACTIVE,
    READ_SCREEN_POLLER_SOURCE,
    RUNTIME_ACTIVITY_EVENT,
    activity_registry,
    is_activity_role,
)

REASON_READ_SCREEN = "read_screen_changed"


async def collect_runtime_activity(body) -> dict:
    """active pulse 1건 수신 → registry pulse 갱신 + WS publish. **DB write/read 0.**

    처리 규칙(DS-110 §6.2, DB 제거판):
      1) activity != active → 422 invalid_activity
      2) Monitor·미지원 role → 422 invalid_role (§12.1)
      3) 직전과 동일 snapshot_hash → idempotent 무시(WS 생략, §12.1)
      4) activity_registry pulse(active, last_active_at, last_activity_hash) 갱신 (인메모리)
      5) message-stream WS 로 runtime_activity_changed 즉시 publish (§7)
    """
    # 1) active 외 거절
    if body.activity != ACTIVITY_ACTIVE:
        raise errors.WebguiError("invalid_activity", 422, "activity must be 'active'.")
    # 2) Monitor·미지원 role 제외
    if not is_activity_role(body.role):
        raise errors.WebguiError(
            "invalid_role", 422, "role is not an activity target (Monitor excluded)."
        )

    observed_iso = body.observed_at.isoformat()

    # 3) 중복 hash 무시: 직전 pulse 와 같은 snapshot_hash 면 WS 생략(idempotent).
    prev = activity_registry.pulse(body.project_id, body.role)
    if prev is not None and prev.last_activity_hash == body.snapshot_hash:
        last_iso = prev.last_active_at.isoformat() if prev.last_active_at else observed_iso
        return {
            "accepted": True,
            "deduplicated": True,
            "project_id": body.project_id,
            "role": body.role,
            "runtime_activity": ACTIVITY_ACTIVE,
            "last_active_at": last_iso,
            "event_id": None,
        }

    # 4) 인메모리 pulse 갱신 (영구 저장 없음 — REST 폴백 rooms 응답이 여기서 읽음)
    activity_registry.set_pulse(
        body.project_id,
        body.role,
        last_active_at=body.observed_at,
        snapshot_hash=body.snapshot_hash,
    )

    # 5) WS fanout — 기존 message-stream 채널 재사용, update_type=runtime_activity_changed (§7).
    # DB event 가 없으므로 event_id 는 휘발성 합성 식별자(UUID). raw screen 미포함.
    event_id = str(uuid.uuid4())
    payload = {
        "project_id": body.project_id,
        "role": body.role,
        "surface_id": body.surface_id,
        "team_session_id": body.team_session_id,
        "runtime_activity": ACTIVITY_ACTIVE,
        "reason": body.reason or REASON_READ_SCREEN,
        "snapshot_hash": body.snapshot_hash,
        "snapshot_bytes": body.snapshot_bytes,
        "poll_interval_ms": body.poll_interval_ms,
        "last_active_at": observed_iso,
    }
    ws_payload = {
        "type": "message_update",
        "cursor": observed_iso,
        "data": {
            "update_id": f"event:{event_id}",
            # liveness 는 DB room 을 만들지 않는다 → room_id 없음. FE 는 (project_id, role) 로 매칭.
            "room_id": None,
            "correlation_id": None,
            "update_type": RUNTIME_ACTIVITY_EVENT,
            "message": None,
            "event": {
                "event_id": event_id,
                "event_type": RUNTIME_ACTIVITY_EVENT,
                "source": READ_SCREEN_POLLER_SOURCE,
                "severity": "info",
                "payload": payload,
                "occurred_at": observed_iso,
            },
            "occurred_at": observed_iso,
        },
    }
    # 프로젝트 전역 구독자(TeamView)에게 fanout. 라우팅 토큰은 (project_id, role) 합성키.
    await hub.publish(f"{body.project_id}:{body.role}", ws_payload, body.project_id)

    return {
        "accepted": True,
        "deduplicated": False,
        "project_id": body.project_id,
        "role": body.role,
        "runtime_activity": ACTIVITY_ACTIVE,
        "last_active_at": observed_iso,
        "event_id": event_id,
    }
