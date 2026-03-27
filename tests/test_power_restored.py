from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import server


def test_online_event_cancels_pending_shutdown(monkeypatch):
    client = server.APP.test_client()
    
    # Track calls
    monkeypatch.setattr(server, "SHARED_TOKEN", "test-token")
    
    # Store mocks
    commands = {"status": "pending", "command": "critical_shutdown", "id": "test-id"}
    def mock_save_cmd(cmd): commands.update(cmd)
    monkeypatch.setattr(server, "save_command", mock_save_cmd)
    monkeypatch.setattr(server, "get_command", lambda: commands)
    
    orch = {"mode": "awaiting_shutdown"}
    monkeypatch.setattr(server, "get_orchestrator_state", lambda: orch)
    def mock_save_orch(o): orch.update(o)
    monkeypatch.setattr(server, "save_orchestrator_state", mock_save_orch)

    # 1. Trigger ONLINE
    resp = client.post(
        "/api/ups/event",
        headers={"X-UPS-Token": "test-token"},
        json={"event": "ONLINE"},
    )
    assert resp.status_code == 200
    assert orch["mode"] == "idle"
    assert commands["status"] == "cancelled"
    assert commands["result"]["reason"] == "power_restored"
