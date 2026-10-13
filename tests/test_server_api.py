def test_health(api):
    resp = api.client.get("/health")
    assert resp.status_code == 200
    assert resp.json["ok"] is True


def test_update_state_requires_token(api):
    resp = api.client.post("/api/ups/server-ups/desktop/update-state", json={})
    assert resp.status_code == 403


def test_update_state_ok(api):
    resp = api.client.post(
        "/api/ups/server-ups/desktop/update-state",
        headers=api.headers,
        json={
            "hostname": "desktop",
            "status": "online",
            "user_active": True,
            "source": "desktop-hook",
        },
    )

    assert resp.status_code == 200
    assert resp.json["ok"] is True
    saved = api.primary.get_desktop_state()
    assert saved["hostname"] == "desktop"
    assert saved["status"] == "online"


def test_get_command_none(api):
    resp = api.client.get("/api/ups/server-ups/desktop/command", headers=api.headers)

    assert resp.status_code == 200
    assert resp.json["command"] is None
