"""WG-ART-04 산출물 변경 polling 엔드포인트 테스트 (DV-70 / DS-40 §20)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.artifact_watcher import ArtifactChangeBuffer, make_cursor


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    proj = tmp_path / "ChProj"
    docs = proj / "documents"
    docs.mkdir(parents=True)
    (proj / ".agiteam").mkdir()
    monkeypatch.setenv("WEBGUI_PROJECT_ID", "ChProj")
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps({"ChProj": str(proj)}))
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    monkeypatch.setenv("WEBGUI_ENABLE_BACKGROUND", "false")
    # 실 watchdog observer 는 켜되, 테스트는 buffer 를 직접 주입해 결정적으로 검증
    monkeypatch.setenv("WEBGUI_ARTIFACT_WATCHER_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c, app
    get_settings.cache_clear()


def test_changes_unavailable_when_watcher_disabled(app_client):
    client, _app = app_client
    r = client.get("/api/webgui/artifacts/changes", params={"project_id": "ChProj"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "artifact_watcher_unavailable"


def test_changes_returns_buffered_updates(app_client):
    client, app = app_client
    # watcher 를 enabled 로 만들고 buffer 에 합성 변경 주입
    watcher = app.state.artifact_watcher
    buf = ArtifactChangeBuffer()
    ts = datetime.now(timezone.utc)
    data = {
        "update_id": "artifact:x:04.development/a.md",
        "project_id": "ChProj",
        "change_type": "modified",
        "path": "04.development/a.md",
        "node_type": "file",
        "parent_path": "04.development",
        "timestamp": ts.isoformat(),
        "event_count": 1,
        "coalesced": False,
    }
    buf.append("ChProj", data, ts, make_cursor(ts, "04.development/a.md"))
    watcher.buffer = buf
    watcher.enabled = True

    r = client.get("/api/webgui/artifacts/changes", params={"project_id": "ChProj"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert len(body["updates"]) == 1
    assert body["updates"][0]["path"] == "04.development/a.md"
    assert body["next_cursor"] is not None

    # project 격리: 타 프로젝트 조회 시 빈 결과
    r2 = client.get("/api/webgui/artifacts/changes", params={"project_id": "Other"})
    assert r2.status_code == 200
    assert r2.json()["data"]["updates"] == []


def test_changes_requires_project_id(app_client):
    client, _app = app_client
    r = client.get("/api/webgui/artifacts/changes")
    # 주의(설계↔구현 불일치 — 아테나 자문 대상): DS-40 §20.4 는 project_id 누락 → 422
    # validation_error 를 명시하나, 이 코드베이스의 전역 RequestValidationError 핸들러
    # (main.py _validation_handler)는 모든 검증오류를 400 invalid_request 로 변환한다.
    # 기존 WG-MSG-04(message_updates)도 동일 거동이므로 시스템 전역 패턴을 따른다.
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_changes_invalid_cursor(app_client):
    client, app = app_client
    app.state.artifact_watcher.enabled = True
    r = client.get(
        "/api/webgui/artifacts/changes",
        params={"project_id": "ChProj", "after": "garbage|artifact:zz"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_pagination"
