"""Tests for observer-role UPS handling.

Covers:
- _power_off_ups builds the correct upscmd argv with auth
- _terminal_action dispatches to upscmd for observer / fsd for primary
- LOWBATT on observer never calls _self_shutdown
- /api/ups/<id>/event response sets shutdown_server=False for observer LOWBATT
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import config  # noqa: E402
from config import (  # noqa: E402
    UPSDeviceConfig,
    DesktopConfig,
    TimingConfig,
    UpscmdConfig,
    ROLE_OBSERVER,
    ROLE_PRIMARY,
)
from ups_context import UPSContext  # noqa: E402


def _make_ctx(role: str, tmp: Path) -> UPSContext:
    device = UPSDeviceConfig(
        id=f"{role}-ups",
        nut_name=f"{role}-ups@localhost",
        role=role,
        timing=TimingConfig(),
        desktop=DesktopConfig(agent_url="http://127.0.0.1:8788"),
        upscmd=UpscmdConfig(shutdown="shutdown.return"),
    )
    return UPSContext(device, tmp)


def _make_primary_and_observer(tmp: Path):
    """A primary + observer sharing the same desktop, with peers registered."""
    primary = _make_ctx(ROLE_PRIMARY, tmp)
    observer = _make_ctx(ROLE_OBSERVER, tmp)
    primary.register_peers([primary, observer])
    observer.register_peers([primary, observer])
    return primary, observer


_SHUTTING_DOWN = {
    "mode": "shutting_down", "phase": "offline_wait",
    "phase_deadline": 0, "onbatt_since": 1, "last_event": "ONBATT", "updated_at": 1,
}


def test_observer_power_off_invokes_upscmd_with_auth(monkeypatch):
    monkeypatch.setattr(config, "UPSCMD_BIN", "/usr/sbin/upscmd")
    monkeypatch.setattr(config, "UPSCMD_USER", "upscmd_admin")
    monkeypatch.setattr(config, "UPSCMD_PASS", "secret")
    # The module-level constants are imported into ups_context — patch there too
    import ups_context
    monkeypatch.setattr(ups_context, "UPSCMD_BIN", "/usr/sbin/upscmd")
    monkeypatch.setattr(ups_context, "UPSCMD_USER", "upscmd_admin")
    monkeypatch.setattr(ups_context, "UPSCMD_PASS", "secret")

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_OBSERVER, Path(tmp))

        result = MagicMock(returncode=0, stderr="")
        with patch("ups_context.subprocess.run", return_value=result) as run:
            assert ctx._power_off_ups() is True

        run.assert_called_once()
        argv = run.call_args[0][0]
        assert argv == [
            "/usr/sbin/upscmd",
            "-u", "upscmd_admin",
            "-p", "secret",
            "observer-ups@localhost",
            "shutdown.return",
        ]


def test_observer_power_off_omits_auth_when_unset(monkeypatch):
    import ups_context
    monkeypatch.setattr(ups_context, "UPSCMD_BIN", "upscmd")
    monkeypatch.setattr(ups_context, "UPSCMD_USER", "")
    monkeypatch.setattr(ups_context, "UPSCMD_PASS", "")

    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_OBSERVER, Path(tmp))

        result = MagicMock(returncode=0, stderr="")
        with patch("ups_context.subprocess.run", return_value=result) as run:
            ctx._power_off_ups()

        argv = run.call_args[0][0]
        assert argv == ["upscmd", "observer-ups@localhost", "shutdown.return"]


def test_observer_terminal_action_uses_upscmd():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_OBSERVER, Path(tmp))
        with patch.object(ctx, "_power_off_ups") as upscmd, \
             patch.object(ctx, "_self_shutdown") as fsd:
            ctx._terminal_action()
            upscmd.assert_called_once()
            fsd.assert_not_called()


def test_primary_terminal_action_uses_fsd():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_PRIMARY, Path(tmp))
        with patch.object(ctx, "_power_off_ups") as upscmd, \
             patch.object(ctx, "_self_shutdown") as fsd:
            ctx._terminal_action()
            fsd.assert_called_once()
            upscmd.assert_not_called()


def test_observer_lowbatt_event_never_calls_self_shutdown(monkeypatch):
    """LOWBATT on an observer UPS must run upscmd, never fsd."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_OBSERVER, Path(tmp))

        called = {"upscmd": 0, "self": 0, "fetch": 0}
        monkeypatch.setattr(ctx, "_power_off_ups", lambda: called.__setitem__("upscmd", called["upscmd"] + 1) or True)
        monkeypatch.setattr(ctx, "_self_shutdown", lambda: called.__setitem__("self", called["self"] + 1))
        monkeypatch.setattr(ctx, "fetch_state_from_desktop", lambda: called.__setitem__("fetch", called["fetch"] + 1) or {})
        monkeypatch.setattr(ctx, "_arm_deadline_timer", lambda delay: None)

        import ups_context as ups_ctx_mod

        class InlineThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target
            def start(self):
                self._target()

        original_thread = ups_ctx_mod.threading.Thread
        monkeypatch.setattr(ups_ctx_mod.threading, "Thread", InlineThread)
        try:
            ctx.handle_event("LOWBATT")
        finally:
            monkeypatch.setattr(ups_ctx_mod.threading, "Thread", original_thread)

        assert called["self"] == 0, "observer must not call _self_shutdown"
        assert called["upscmd"] == 1, "observer must call _power_off_ups exactly once"
        assert called["fetch"] == 0, "observer must not fetch desktop state at ONBATT"


def test_observer_onbatt_never_prompts_desktop(monkeypatch):
    """ONBATT on observer must not push any command to the desktop."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_OBSERVER, Path(tmp))

        pushed = []
        monkeypatch.setattr(ctx, "push_command_to_desktop", lambda cmd: pushed.append(cmd))
        monkeypatch.setattr(ctx, "fetch_state_from_desktop", lambda: {})

        ctx.handle_event("ONBATT")

        assert pushed == [], "observer must not push any command on ONBATT"


def test_observer_ignores_desktop_state_change(monkeypatch):
    """notify_desktop_state_change must be a no-op for observer — even if desktop
    reports 'offline', observer must not independently trigger terminal action."""
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(ROLE_OBSERVER, Path(tmp))

        # Put context in monitoring mode as if ONBATT was received
        ctx.save_orchestrator_state({
            "mode": "monitoring_battery",
            "phase": "offline_wait",
            "phase_deadline": 9999999999,
            "onbatt_since": 1,
            "last_event": "ONBATT",
            "updated_at": 1,
        })

        terminal_called = []
        monkeypatch.setattr(ctx, "_terminal_action", lambda: terminal_called.append(True))

        ctx.notify_desktop_state_change("offline")

        assert terminal_called == [], "observer must not trigger terminal action on desktop state change"


def test_observer_powers_off_once_primary_reports_desktop_down(monkeypatch):
    """Graceful observer power-off must hold until the primary that owns the
    desktop reports it is down, then power off — never before."""
    with tempfile.TemporaryDirectory() as tmp:
        primary, observer = _make_primary_and_observer(Path(tmp))
        observer.save_orchestrator_state(dict(_SHUTTING_DOWN))
        # The desktop pushed 'shutting_down' to the PRIMARY's ups_id.
        primary.save_desktop_state({"status": "shutting_down"})

        powered_off = []
        monkeypatch.setattr(observer, "_power_off_ups", lambda: powered_off.append(True) or True)

        observer._observer_power_off_when_desktop_safe()

        assert powered_off == [True], "observer must power off once desktop is confirmed down"


def test_observer_holds_power_off_while_desktop_up_then_aborts_on_restore(monkeypatch):
    """While the desktop is still alive, the observer must not power off; if power
    is restored (mode leaves shutting_down) it aborts without cutting power."""
    with tempfile.TemporaryDirectory() as tmp:
        primary, observer = _make_primary_and_observer(Path(tmp))
        # Desktop still online — must never be a reason to power off.
        primary.save_desktop_state({"status": "online"})
        # Power restored: orchestrator no longer in shutting_down.
        observer.save_orchestrator_state({
            "mode": "idle", "phase": None, "phase_deadline": None,
            "onbatt_since": None, "last_event": "ONLINE", "updated_at": 1,
        })

        powered_off = []
        monkeypatch.setattr(observer, "_power_off_ups", lambda: powered_off.append(True))

        observer._observer_power_off_when_desktop_safe()

        assert powered_off == [], "observer must not cut power when desktop is up / power restored"


def test_observer_lowbatt_backstop_powers_off_while_waiting(monkeypatch):
    """If this UPS hits low battery while waiting for the desktop, power off."""
    with tempfile.TemporaryDirectory() as tmp:
        primary, observer = _make_primary_and_observer(Path(tmp))
        observer.save_orchestrator_state(dict(_SHUTTING_DOWN))
        primary.save_desktop_state({"status": "online"})  # desktop never goes down
        monkeypatch.setattr(observer, "read_ups_status", lambda: "OB LB")

        powered_off = []
        monkeypatch.setattr(observer, "_power_off_ups", lambda: powered_off.append(True))

        observer._observer_power_off_when_desktop_safe()

        assert powered_off == [True], "low battery must force power off as a backstop"


def test_observer_without_owning_primary_powers_off_immediately(monkeypatch):
    """A standalone observer (no primary coordinates its desktop) keeps the old
    behaviour: power off without waiting."""
    with tempfile.TemporaryDirectory() as tmp:
        observer = _make_ctx(ROLE_OBSERVER, Path(tmp))
        observer.register_peers([observer])  # no primary peer
        observer.save_orchestrator_state(dict(_SHUTTING_DOWN))

        powered_off = []
        monkeypatch.setattr(observer, "_power_off_ups", lambda: powered_off.append(True))

        observer._observer_power_off_when_desktop_safe()

        assert powered_off == [True]


def test_observer_phase_deadline_defers_lowbatt_powers_off_now(monkeypatch):
    """_execute_phase_action: graceful (phase_deadline) → deferred worker;
    low battery → immediate terminal action."""
    with tempfile.TemporaryDirectory() as tmp:
        primary, observer = _make_primary_and_observer(Path(tmp))
        observer.save_orchestrator_state({
            "mode": "monitoring_battery", "phase": "offline_wait",
            "phase_deadline": 0, "onbatt_since": 1, "last_event": "ONBATT", "updated_at": 1,
        })

        import ups_context as ups_ctx_mod

        class InlineThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target
            def start(self):
                self._target()

        calls = []
        monkeypatch.setattr(observer, "_observer_power_off_when_desktop_safe",
                            lambda: calls.append("deferred"))
        monkeypatch.setattr(observer, "_terminal_action", lambda: calls.append("immediate"))
        monkeypatch.setattr(ups_ctx_mod.threading, "Thread", InlineThread)

        observer._execute_phase_action(reason="phase_deadline")
        assert calls == ["deferred"], "timer expiry must defer, not cut power"

        # Reset to monitoring and fire low-battery → immediate power off.
        observer.save_orchestrator_state({
            "mode": "monitoring_battery", "phase": "offline_wait",
            "phase_deadline": 0, "onbatt_since": 1, "last_event": "ONBATT", "updated_at": 1,
        })
        observer._execute_phase_action(reason="low_battery")
        assert calls == ["deferred", "immediate"], "low battery must power off immediately"


def test_event_api_returns_shutdown_server_flag(monkeypatch):
    """The /api/ups/<id>/event response must tell upssched-cmd whether to fire
    the safety-net shell shutdown — true only for primary LOWBATT."""
    monkeypatch.setenv("UPS_CONFIG_FILE", str(ROOT / "server" / "ups_config.example.yml"))
    monkeypatch.setenv("UPS_STATE_DIR", tempfile.mkdtemp(prefix="ups-test-"))
    monkeypatch.setenv("UPS_LOGS_DIR", tempfile.mkdtemp(prefix="ups-test-"))

    # Force a fresh import so the env vars are picked up
    for mod in ("server", "config", "ups_context"):
        sys.modules.pop(mod, None)
    import server  # noqa: E402

    # Block any threads that would try real shutdown
    for ctx in server.CONTEXTS.values():
        ctx._self_shutdown = lambda: None
        ctx._power_off_ups = lambda: True
        ctx.fetch_state_from_desktop = lambda: {}

    client = server.APP.test_client()
    headers = {"X-UPS-Token": server.SHARED_TOKEN, "Content-Type": "application/json"}

    # Primary LOWBATT → shutdown_server: true
    r = client.post("/api/ups/server-ups/event", json={"event": "LOWBATT"}, headers=headers)
    assert r.status_code == 200
    assert r.get_json()["shutdown_server"] is True

    # Observer LOWBATT → shutdown_server: false
    r = client.post("/api/ups/desktop-ups/event", json={"event": "LOWBATT"}, headers=headers)
    assert r.status_code == 200
    assert r.get_json()["shutdown_server"] is False

    # ONBATT (non-shutdown event) → shutdown_server: false regardless of role
    r = client.post("/api/ups/server-ups/event", json={"event": "ONBATT"}, headers=headers)
    assert r.status_code == 200
    assert r.get_json()["shutdown_server"] is False
