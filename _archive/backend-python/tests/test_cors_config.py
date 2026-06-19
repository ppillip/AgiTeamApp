from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_cors_allow_origins_accepts_configured_ip_origin(tmp_path, monkeypatch):
    project_root = tmp_path / "Panthea"
    (project_root / "documents").mkdir(parents=True)

    monkeypatch.setenv("WEBGUI_PROJECT_ID", "Panthea")
    monkeypatch.setenv("WEBGUI_PROJECT_ROOTS_JSON", json.dumps({"Panthea": str(project_root)}))
    monkeypatch.setenv("WEBGUI_ENABLE_BACKGROUND", "false")
    monkeypatch.delenv("WEBGUI_API_TOKEN", raising=False)
    monkeypatch.delenv("WEBGUI_COLLECTOR_TOKEN", raising=False)
    monkeypatch.setenv(
        "WEBGUI_CORS_ALLOW_ORIGINS",
        "http://localhost:1420,http://127.0.0.1:1420,http://192.168.0.10:1420",
    )

    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.options(
            "/healthz",
            headers={
                "Origin": "http://192.168.0.10:1420",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://192.168.0.10:1420"
    get_settings.cache_clear()
