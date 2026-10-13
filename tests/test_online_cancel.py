from ups_context import MODE_IDLE, MODE_MONITORING, PHASE_USER_PROMPT


def test_online_should_clear_pending_ups_state_prompt(api):
    """Power back while the desktop prompt is still open: the prompt is withdrawn
    and the orchestrator stands down."""
    api.primary.save_orchestrator_state({
        "mode": MODE_MONITORING,
        "phase": PHASE_USER_PROMPT,
        "phase_deadline": 9999999999,
        "onbatt_since": 1,
        "last_event": "ONBATT",
    })
    api.primary.save_command({"id": "state-1", "command": "ups_state", "status": "pending"})

    resp = api.client.post(
        "/api/ups/server-ups/event",
        headers=api.headers,
        json={"event": "ONLINE"},
    )

    assert resp.status_code == 200
    assert api.primary.get_command()["status"] == "cancelled"
    orch = api.primary.get_orchestrator_state()
    assert orch["mode"] == MODE_IDLE
    assert orch["last_event"] == "ONLINE"
