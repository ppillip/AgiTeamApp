"""A-F1 후속 2026-06-10: WG-MSG-05 message-stream 은 project_id 필수.

project_id 없는 WS 구독은 hub 전역구독이 되어 타 프로젝트 push 까지 받으므로 거절(4400).
room_id 의 cross-project 방어검증은 DB 의존이라 hub 단위테스트(test_events_project_isolation)
+ message-updates 통합테스트로 커버한다.
"""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect


def test_message_stream_rejects_without_project_id(client):
    """project_id 미전달 시 핸드셰이크가 거절된다(close 4400)."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/webgui/message-stream") as ws:
            ws.receive_json()


def test_message_stream_accepts_with_project_id(client):
    """project_id 만으로 전역구독(room_id 없음)은 accept 된다(DB 미접근 경로)."""
    with client.websocket_connect("/api/webgui/message-stream?project_id=TestProj") as ws:
        # 핸드셰이크 수립 확인 후 즉시 종료.
        assert ws is not None
