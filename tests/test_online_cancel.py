from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import server


def test_online_should_clear_pending_soft_suspend(monkeypatch):
    client = server.APP.test_client()

    saved = {}
    saved_orch = {}

    monkeypatch.setattr(
        server,
        "get_desktop_state",
        lambda: {
            "status": "online",
            "last_seen": server.now_ts(),
        },
    )

    monkeypatch.setattr(
        server,
        "get_command",
        lambda: {
            "id": "soft-1",
            "command": "soft_suspend",
            "status": "pending",
        },
    )

    monkeypatch.setattr(server, "save_command", lambda cmd: saved.update(cmd))
    monkeypatch.setattr(
        server, "save_orchestrator_state", lambda orch: saved_orch.update(orch)
    )
    monkeypatch.setattr(
        server,
        "get_orchestrator_state",
        lambda: {
            "mode": "on_battery_waiting",
            "last_event": "ONBATT",
            "pending_command": None,
            "suspend_deadline": server.now_ts() + 300,
            "updated_at": server.now_ts(),
        },
    )

    resp = client.post(
        "/api/ups/event",
        headers={"X-UPS-Token": server.SHARED_TOKEN},
        json={"event": "ONLINE"},
    )

    assert resp.status_code == 200
    assert saved["status"] == "cancelled"
    assert saved_orch["mode"] == "idle"
    assert saved_orch["last_event"] == "ONLINE"
