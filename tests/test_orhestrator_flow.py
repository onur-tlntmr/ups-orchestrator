from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import server


def test_lowbatt_when_suspended_sets_shutdown_pending(monkeypatch):
    client = server.APP.test_client()

    saved_command = {}

    monkeypatch.setattr(server, "get_desktop_state", lambda: {
        "status": "suspended",
        "last_seen": server.now_ts(),
    })

    monkeypatch.setattr(server, "state_is_fresh", lambda state: True)

    monkeypatch.setattr(server, "fetch_state_from_desktop", lambda: {
        "status": "online",
        "last_seen": server.now_ts(),
    })

    def fake_save_command(cmd):
        saved_command.update(cmd)

    monkeypatch.setattr(server, "save_command", fake_save_command)

    resp = client.post(
        "/api/ups/event",
        headers={"X-UPS-Token": server.SHARED_TOKEN},
        json={"event": "LOWBATT"},
    )

    assert resp.status_code == 200
    assert saved_command["command"] == "critical_shutdown"
    assert saved_command["status"] == "pending"


def test_onbatt_sets_ups_state(monkeypatch):
    client = server.APP.test_client()

    saved_command = {}

    monkeypatch.setattr(server, "get_desktop_state", lambda: {
        "status": "online",
        "last_seen": server.now_ts(),
    })

    monkeypatch.setattr(server, "save_command", lambda cmd: saved_command.update(cmd))

    resp = client.post(
        "/api/ups/event",
        headers={"X-UPS-Token": server.SHARED_TOKEN},
        json={"event": "ONBATT"},
    )

    assert resp.status_code == 200
    assert saved_command["command"] == "ups_state"
    assert saved_command["payload"] == {"event": "ONBATT"}
    assert saved_command["status"] == "pending"