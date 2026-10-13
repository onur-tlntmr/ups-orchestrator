from ups_context import MODE_IDLE, MODE_SHUTTING_DOWN, PHASE_SUSPEND_WAIT


def test_online_event_cancels_pending_shutdown(api):
    """Power back after critical_shutdown was issued but before the desktop ran it:
    the command is cancelled so the desktop's preflight refuses it."""
    api.primary.save_orchestrator_state({
        "mode": MODE_SHUTTING_DOWN,
        "phase": PHASE_SUSPEND_WAIT,
        "phase_deadline": 1,
        "onbatt_since": 1,
        "last_event": "LOWBATT",
    })
    api.primary.save_command({"id": "shutdown-1", "command": "critical_shutdown", "status": "pending"})

    resp = api.client.post(
        "/api/ups/server-ups/event",
        headers=api.headers,
        json={"event": "ONLINE"},
    )

    assert resp.status_code == 200
    assert api.primary.get_orchestrator_state()["mode"] == MODE_IDLE
    cmd = api.primary.get_command()
    assert cmd["status"] == "cancelled"
    assert cmd["result"]["reason"] == "power_restored_event"
