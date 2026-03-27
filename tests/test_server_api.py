from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import server


def test_health():
    client = server.APP.test_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json["ok"] is True


def test_update_state_requires_token():
    client = server.APP.test_client()
    resp = client.post("/api/desktop/update-state", json={})
    assert resp.status_code == 403


def test_update_state_ok(monkeypatch):
    client = server.APP.test_client()

    saved = {}

    def fake_save(state):
        saved.update(state)

    monkeypatch.setattr(server, "save_desktop_state", fake_save)

    resp = client.post(
        "/api/desktop/update-state",
        headers={"X-UPS-Token": server.SHARED_TOKEN},
        json={
            "hostname": "desktop",
            "status": "online",
            "user_active": True,
            "source": "desktop-hook",
        },
    )

    assert resp.status_code == 200
    assert resp.json["ok"] is True
    assert saved["hostname"] == "desktop"
    assert saved["status"] == "online"


def test_get_command_none(monkeypatch):
    client = server.APP.test_client()

    monkeypatch.setattr(server, "get_command", lambda: {})

    resp = client.get(
        "/api/desktop/command",
        headers={"X-UPS-Token": server.SHARED_TOKEN},
    )

    assert resp.status_code == 200
    assert resp.json["command"] is None