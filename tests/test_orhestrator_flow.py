from ups_context import MODE_MONITORING, PHASE_USER_PROMPT


def test_lowbatt_when_suspended_sets_shutdown_pending(api):
    api.primary.save_desktop_state({"status": "suspended"})
    # Unreachable while asleep, online once woken.
    api.desktop_replies = [{}, {"status": "online"}]

    resp = api.client.post(
        "/api/ups/server-ups/event",
        headers=api.headers,
        json={"event": "LOWBATT"},
    )

    assert resp.status_code == 200
    cmd = api.primary.get_command()
    assert cmd["command"] == "critical_shutdown"
    assert cmd["status"] == "pending"


def test_onbatt_sets_ups_state(api):
    api.desktop_replies = [{"status": "online"}]

    resp = api.client.post(
        "/api/ups/server-ups/event",
        headers=api.headers,
        json={"event": "ONBATT"},
    )

    assert resp.status_code == 200
    cmd = api.primary.get_command()
    assert cmd["command"] == "ups_state"
    assert cmd["payload"] == {"event": "ONBATT"}
    assert cmd["status"] == "pending"
    assert ("push", "ups_state") in api.calls
    orch = api.primary.get_orchestrator_state()
    assert orch["mode"] == MODE_MONITORING
    assert orch["phase"] == PHASE_USER_PROMPT
