"""기존 이미지첨부 중복분 정리 (결함수정 2026-06-14, PM 지시 Part 3).

원리: transcript 출처 outbound 중 [Image: source:]/codex 합성 블록이 박힌 것 가운데,
같은 방에 'bridge(webgui) 짝'(첨부 보유 + 본문 canonical 일치)이 존재하는 것만 삭제 대상.
수정 코드(_find_outbound_text_dup)와 '같은 매칭 함수'를 재사용해 드리프트를 막는다.

사용:
  python -m scripts.cleanup_image_dupe            # dry-run (식별만, 삭제 안 함)
  python -m scripts.cleanup_image_dupe --apply    # 실제 삭제
  옵션: --room <room_id_prefix>  특정 방으로 한정(기본=전체 방)
"""
from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import WebguiMessage
from app.services.pm_bridge import _SUBMIT_ATTACHMENT_MARKERS, strip_submit_attachment_suffix
from app.services.transcript_collector import canonical_match_text

_BRIDGE_SOURCES = ("webgui", "pm_bridge", "bridge")


def _db_url() -> str:
    url = os.environ.get("WEBGUI_DATABASE_URL")
    if not url:
        url = "postgresql+psycopg://agiteamapp:change-me-local-only@localhost:15432/agiteamapp"
    return url


def _bridge_twin(cand_text: str, bridges: list[WebguiMessage]) -> WebguiMessage | None:
    """수정 코드와 동일 규칙: 합성 블록 제거 후 canonical 일치(또는 이미지 전용=빈본문+첨부)."""
    raw = cand_text or ""
    target = canonical_match_text(strip_submit_attachment_suffix(raw))
    had_marker = any(m in raw for m in _SUBMIT_ATTACHMENT_MARKERS)
    if not target and not had_marker:
        return None
    for b in bridges:
        bkey = canonical_match_text(strip_submit_attachment_suffix(b.normalized_text))
        if target:
            if bkey == target:
                return b
        else:
            if not bkey and b.attachments_json:
                return b
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제 삭제 수행(기본 dry-run)")
    ap.add_argument("--room", default=None, help="room_id prefix 한정")
    args = ap.parse_args()

    engine = create_async_engine(_db_url())
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with sm() as db:
        # 후보: transcript 출처 outbound 중 합성 마커 보유
        q = select(WebguiMessage).where(
            WebguiMessage.direction == "outbound",
            WebguiMessage.source == "transcript",
        ).order_by(WebguiMessage.recorded_at)
        rows = (await db.execute(q)).scalars().all()

        # 방별 bridge 후보 캐시
        bridges_by_room: dict[str, list[WebguiMessage]] = {}

        async def bridges_for(room_id) -> list[WebguiMessage]:
            key = str(room_id)
            if key not in bridges_by_room:
                bq = select(WebguiMessage).where(
                    WebguiMessage.room_id == room_id,
                    WebguiMessage.direction == "outbound",
                    WebguiMessage.source.in_(_BRIDGE_SOURCES),
                ).order_by(WebguiMessage.recorded_at.desc())
                bridges_by_room[key] = (await db.execute(bq)).scalars().all()
            return bridges_by_room[key]

        to_delete: list[tuple[WebguiMessage, WebguiMessage]] = []
        held_empty: list[WebguiMessage] = []   # 빈본문(이미지전용): 1:1 짝 보장 불가 → 보류
        claimed_bridge: set[str] = set()       # bridge 1:1 강제(한 짝이 여러 transcript에 재사용 방지)
        for m in rows:
            if args.room and not str(m.room_id).startswith(args.room):
                continue
            if not any(mk in (m.normalized_text or "") for mk in _SUBMIT_ATTACHMENT_MARKERS):
                continue
            target = canonical_match_text(strip_submit_attachment_suffix(m.normalized_text))
            if not target:
                # 본문 없는 이미지전용 합성본: 텍스트 키가 없어 1:1 짝을 확신할 수 없다(과매칭 위험).
                # 자동 삭제 제외 — 수동 검토 대상으로만 보고.
                held_empty.append(m)
                continue
            twin = _bridge_twin(m.normalized_text, await bridges_for(m.room_id))
            if twin is None:
                continue
            bkey = str(twin.message_id)
            if bkey in claimed_bridge:
                # 이 bridge 는 이미 다른 transcript 짝에 귀속됨 → 추가 매칭은 보류(과매칭 차단).
                held_empty.append(m)
                continue
            claimed_bridge.add(bkey)
            to_delete.append((m, twin))

        print(f"=== [삭제 대상] 텍스트 일치 + bridge 1:1 짝 확인: {len(to_delete)}건 ===")
        for m, twin in to_delete:
            txt = canonical_match_text(strip_submit_attachment_suffix(m.normalized_text))[:50]
            print(f"  DEL {str(m.message_id)[:8]} (transcript) ↔ keep {str(twin.message_id)[:8]} (bridge, att={'Y' if twin.attachments_json else 'N'})  '{txt}'")
        print(f"\n=== [보류] 1:1 짝 불확실(이미지전용/중복귀속) — 자동삭제 제외, 수동검토: {len(held_empty)}건 ===")
        for m in held_empty:
            print(f"  HOLD {str(m.message_id)[:8]} (transcript)  occurred={m.occurred_at}")

        if args.apply and to_delete:
            # 삭제 직전 백업(백업=삭제집합 보장). 복구 가능하도록 전 컬럼 보존.
            import json
            from datetime import datetime, timezone

            cols = [c.name for c in WebguiMessage.__table__.columns]
            backup = []
            for m, twin in to_delete:
                row = {c: getattr(m, c) for c in cols}
                row["_kept_bridge_message_id"] = str(twin.message_id)
                backup.append(row)
            os.makedirs("_backup", exist_ok=True)
            suffix = f"_room-{args.room}" if args.room else "_all"
            path = f"_backup/deleted_image_dupe_20260614{suffix}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False, default=str, indent=2)
            print(f"\n백업 저장: {path} ({len(backup)}건)")

            for m, _ in to_delete:
                await db.delete(m)
            await db.commit()
            print(f"*** 삭제 완료: {len(to_delete)}건 ***")
        elif not args.apply:
            print("\n(dry-run: 삭제 안 함. --apply 로 실제 삭제)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
