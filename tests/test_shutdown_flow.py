def test_shutdown_sequence_waits_for_online(api):
    """LOWBATT with the desktop asleep: wake it, push critical_shutdown only once it
    reports online, and shut the server down after the desktop confirms."""
    api.primary.save_desktop_state({"status": "suspended"})
    # Unreachable at LOWBATT and on the first wake check, then online.
    api.desktop_replies = [{}, {}, {"status": "online"}]

    resp = api.client.post(
        "/api/ups/server-ups/event",
        headers=api.headers,
        json={"event": "LOWBATT"},
    )

    assert resp.status_code == 200
    assert resp.json["shutdown_server"] is True
    assert api.calls == [
        ("fetch", None),        # LOWBATT: desktop unreachable → suspend_wait
        ("wol",),
        ("fetch", None),        # still asleep: nothing pushed yet
        ("fetch", "online"),
        ("push", "critical_shutdown"),
        ("fsd",),               # desktop reported shutting_down → server goes down
    ]
