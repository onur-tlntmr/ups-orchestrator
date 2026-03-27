from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import server


def test_shutdown_sequence_waits_for_online(monkeypatch):
    client = server.APP.test_client()
    
    # Track calls
    pushed_commands = []
    
    # Mocking
    monkeypatch.setattr(server, "SHARED_TOKEN", "test-token")
    monkeypatch.setattr(server, "get_desktop_state", lambda: {"status": "suspended"})
    monkeypatch.setattr(server, "push_command_to_desktop", lambda cmd: pushed_commands.append(cmd))
    
    # Store mocks
    commands = {}
    def mock_save_cmd(cmd): commands.update(cmd)
    monkeypatch.setattr(server, "save_command", mock_save_cmd)
    monkeypatch.setattr(server, "get_command", lambda: commands)
    
    orch = {"mode": "idle"}
    monkeypatch.setattr(server, "get_orchestrator_state", lambda: orch)
    def mock_save_orch(o): orch.update(o)
    monkeypatch.setattr(server, "save_orchestrator_state", mock_save_orch)

    # 1. Trigger LOWBATT
    resp = client.post(
        "/api/ups/event",
        headers={"X-UPS-Token": "test-token"},
        json={"event": "LOWBATT"},
    )
    assert resp.status_code == 200
    assert orch["mode"] == "awaiting_shutdown"
    assert len(pushed_commands) == 0  # Should NOT push yet because state was suspended

    # 2. Desktop reports "online"
    resp = client.post(
        "/api/desktop/update-state",
        headers={"X-UPS-Token": "test-token"},
        json={"status": "online", "hostname": "desktop"}
    )
    assert resp.status_code == 200
    assert len(pushed_commands) == 1
    assert pushed_commands[0]["command"] == "critical_shutdown"
