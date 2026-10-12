"""Outage on the desktop's UPS only (observer on battery, primary on mains).

Regression for 2026-10-12: desktop-ups went on battery while server-ups kept
reporting OL. Desktop coordination only started on the primary's ONBATT, so the
suspended desktop was never woken or shut down; the observer just held its
power-off for desktop_offline_wait + observer_poweroff_max_wait (35 min).

These tests drive the real poll path the way server.poll_loop does — every POLL
seconds each context is polled in its own thread (handle_ups_status_transition,
or handle_ups_status_unavailable when upsc fails) — with real worker threads on
a scaled clock, so the primary's and observer's workers run concurrently as they
do in production.
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import ups_context  # noqa: E402
from config import (  # noqa: E402
    UPSDeviceConfig,
    DesktopConfig,
    TimingConfig,
    ROLE_OBSERVER,
    ROLE_PRIMARY,
)
from ups_context import MODE_IDLE, MODE_MONITORING  # noqa: E402

POLL = 30     # simulated seconds between polls (server UNKNOWN_POLL_INTERVAL)
SPEED = 400   # simulated seconds per real second
TIMING = TimingConfig()

_real_sleep = time.sleep
_RealTimer = threading.Timer


class DualUpsSimulation:
    """Primary + observer sharing one desktop, driven on a scaled clock."""

    def __init__(self, monkeypatch, desktop_status: str):
        self._threads_before = set(threading.enumerate())
        origin = time.time()
        self.now = lambda: int(1_000_000 + (time.time() - origin) * SPEED)
        monkeypatch.setattr(ups_context, "now_ts", self.now)
        monkeypatch.setattr(ups_context.time, "sleep", lambda s: _real_sleep(s / SPEED))
        monkeypatch.setattr(ups_context.threading, "Timer", lambda d, fn: _RealTimer(d / SPEED, fn))

        tmp = Path(tempfile.mkdtemp(prefix="ups-test-"))
        monkeypatch.setattr(ups_context, "LOGS_DIR", tmp / "logs")
        desktop = DesktopConfig(agent_url="http://127.0.0.1:8788", mac_address="AA:BB:CC:DD:EE:FF")
        self.primary = ups_context.UPSContext(
            UPSDeviceConfig("server-ups", "server-ups@localhost", ROLE_PRIMARY, TimingConfig(), desktop), tmp)
        self.observer = ups_context.UPSContext(
            UPSDeviceConfig("desktop-ups", "desktop-ups@localhost", ROLE_OBSERVER, TimingConfig(), desktop), tmp)
        for ctx in (self.primary, self.observer):
            ctx.register_peers([self.primary, self.observer])
            ctx.reset_state_on_startup()

        # The desktop pushes its state to the primary's ups_id only.
        self.primary.save_desktop_state({"status": desktop_status})

        self.events = []
        self.t0 = self.now()
        for ctx in (self.primary, self.observer):
            # An online desktop answers /state; a suspended one is unreachable.
            monkeypatch.setattr(ctx, "fetch_state_from_desktop",
                                lambda: {"status": "online"} if desktop_status == "online" else {})
            monkeypatch.setattr(ctx, "_send_wol", lambda c=ctx: self._record(c, "wol") or True)
            monkeypatch.setattr(ctx, "push_command_to_desktop", lambda cmd, c=ctx: self._on_push(c, cmd))
            monkeypatch.setattr(ctx, "_power_off_ups", lambda c=ctx: self._record(c, "upscmd") or True)
            monkeypatch.setattr(ctx, "_self_shutdown", lambda c=ctx: self._record(c, "fsd"))

        # None = upsc unavailable for that UPS.
        self.statuses = {self.primary: "OL", self.observer: "OL"}
        self.desktop_obeys_shutdown = False

    def _record(self, ctx, kind):
        self.events.append((self.now() - self.t0, ctx.device.id, kind))

    def _on_push(self, ctx, cmd):
        self._record(ctx, cmd["command"])
        if cmd["command"] == "critical_shutdown" and self.desktop_obeys_shutdown:
            # Desktop agent reports 'shutting_down' to the primary's update-state route.
            self.primary.save_desktop_state({"status": "shutting_down"})
            self.primary.notify_desktop_state_change("shutting_down")
        return True

    def _poll(self, ctx):
        """Mirrors server._poll_device."""
        status = self.statuses[ctx]
        if status:
            ctx.handle_ups_status_transition(status)
        else:
            ctx.handle_ups_status_unavailable()

    def run_until(self, sim_seconds: int):
        while self.now() - self.t0 < sim_seconds:
            threads = [threading.Thread(target=self._poll, args=(c,)) for c in (self.primary, self.observer)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            _real_sleep(POLL / SPEED)

    def first(self, kind, ups_id=None):
        return next((t for t, u, k in self.events if k == kind and ups_id in (None, u)), None)

    def count(self, kind, ups_id=None):
        return sum(1 for _, u, k in self.events if k == kind and ups_id in (None, u))

    def stop(self):
        """Abort background workers before monkeypatches are undone."""
        for ctx in (self.primary, self.observer):
            ctx._reset_to_idle("test_end")
        workers = [t for t in threading.enumerate() if t not in self._threads_before]
        for t in workers:
            t.join(timeout=2)
        leaked = [t for t in workers if t.is_alive()]
        assert not leaked, f"worker threads still running: {leaked}"


# The primary sees the observer go on battery at most one poll later.
WAKE_BUDGET = TIMING.desktop_suspend_wait + 2 * POLL


def test_suspended_desktop_is_woken_and_shut_down_when_only_desktop_ups_on_battery(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.desktop_obeys_shutdown = True
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(TIMING.desktop_suspend_wait + 300)
    finally:
        sim.stop()

    wol = sim.first("wol", "server-ups")
    assert wol is not None and wol <= WAKE_BUDGET, sim.events
    assert sim.first("critical_shutdown", "server-ups") is not None, sim.events
    # Desktop confirmed down → observer powers its UPS off without the 1800 s backstop.
    assert sim.first("upscmd", "desktop-ups") is not None, sim.events
    # Server's own UPS is on mains — the server must stay up.
    assert sim.first("fsd") is None, sim.events


def test_online_desktop_is_prompted_when_only_desktop_ups_on_battery(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="online")
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(3 * POLL)
    finally:
        sim.stop()

    assert sim.first("ups_state", "server-ups") is not None, sim.events


def test_desktop_is_coordinated_even_when_server_ups_status_is_unavailable(monkeypatch):
    """On 2026-10-12 server-ups never reported OB even though it shared the mains —
    a blind primary UPS must not stop the desktop from being handled."""
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.statuses[sim.primary] = None
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(WAKE_BUDGET + POLL)
    finally:
        sim.stop()

    assert sim.first("wol", "server-ups") is not None, sim.events
    assert sim.first("fsd") is None, sim.events


def test_desktop_ups_power_restored_cancels_desktop_only_coordination(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(3 * POLL)
        assert sim.primary.get_orchestrator_state()["mode"] == MODE_MONITORING
        sim.statuses[sim.observer] = "OL"
        sim.run_until(TIMING.desktop_suspend_wait + 120)
    finally:
        sim.stop()

    assert sim.primary.get_orchestrator_state()["mode"] == MODE_IDLE
    assert sim.first("wol") is None, sim.events
    assert sim.first("critical_shutdown") is None, sim.events


def test_server_ups_restored_first_keeps_coordinating_desktop(monkeypatch):
    """Both UPSes on battery, server's mains returns first: the desktop's UPS is
    still on battery, so the desktop must still be woken and shut down — and the
    server, now on mains, must not shut itself down."""
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.desktop_obeys_shutdown = True
    sim.statuses[sim.primary] = "OB DISCHRG"
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(2 * POLL)
        sim.statuses[sim.primary] = "OL"
        sim.run_until(TIMING.desktop_suspend_wait + 300)
    finally:
        sim.stop()

    assert sim.first("wol", "server-ups") is not None, sim.events
    assert sim.first("critical_shutdown", "server-ups") is not None, sim.events
    assert sim.first("fsd") is None, sim.events
    assert sim.first("upscmd", "desktop-ups") is not None, sim.events


def test_primary_going_on_battery_during_desktop_only_still_shuts_server_down(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.desktop_obeys_shutdown = True
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(2 * POLL)
        sim.statuses[sim.primary] = "OB DISCHRG"
        sim.run_until(TIMING.desktop_suspend_wait + 300)
    finally:
        sim.stop()

    assert sim.first("critical_shutdown", "server-ups") is not None, sim.events
    assert sim.first("fsd", "server-ups") is not None, sim.events


def test_primary_going_on_battery_after_desktop_only_finished_still_shuts_server_down(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.desktop_obeys_shutdown = True
    sim.statuses[sim.observer] = "OB DISCHRG"
    try:
        sim.run_until(TIMING.desktop_suspend_wait + 180)
        assert sim.first("critical_shutdown", "server-ups") is not None, sim.events
        assert sim.first("fsd") is None, sim.events
        sim.statuses[sim.primary] = "OB DISCHRG"
        sim.run_until(TIMING.desktop_suspend_wait + 180 + TIMING.desktop_offline_wait + 2 * POLL)
    finally:
        sim.stop()

    assert sim.first("fsd", "server-ups") is not None, sim.events


def test_observer_stops_holding_once_primary_gave_up_on_desktop(monkeypatch):
    """Wake fails, desktop never confirms down: once the primary has finished its
    desktop flow there is nothing left to wait for, so the observer must not keep
    holding until observer_poweroff_max_wait."""
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.statuses[sim.observer] = "OB DISCHRG"
    primary_done = (WAKE_BUDGET + TIMING.wake_online_timeout + TIMING.desktop_shutdown_wait)
    try:
        sim.run_until(primary_done + 2 * POLL)
    finally:
        sim.stop()

    upscmd = sim.first("upscmd", "desktop-ups")
    assert upscmd is not None and upscmd <= primary_done + POLL, sim.events
    assert sim.first("fsd") is None, sim.events


def test_wol_is_resent_every_retry_interval_until_wake_timeout(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.statuses[sim.primary] = "OB DISCHRG"
    try:
        sim.run_until(TIMING.desktop_suspend_wait + TIMING.wake_online_timeout + POLL)
    finally:
        sim.stop()

    # wake_online_timeout=60, wol_retry_interval=20 → sends at 0, 20, 40 s.
    expected = TIMING.wake_online_timeout // TIMING.wol_retry_interval
    assert sim.count("wol", "server-ups") == expected, sim.events


def test_wol_sent_once_when_retry_disabled(monkeypatch):
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.primary.device.timing.wol_retry_interval = 0
    sim.statuses[sim.primary] = "OB DISCHRG"
    try:
        sim.run_until(TIMING.desktop_suspend_wait + TIMING.wake_online_timeout + POLL)
    finally:
        sim.stop()

    assert sim.count("wol", "server-ups") == 1, sim.events


def test_still_suspended_desktop_is_not_taken_as_shutdown_confirmation(monkeypatch):
    """After the wake attempt a desktop still reporting 'suspended' never received
    the shutdown — the primary must wait the full desktop_shutdown_wait before
    shutting the server down, not treat 'suspended' as confirmation."""
    sim = DualUpsSimulation(monkeypatch, desktop_status="suspended")
    sim.statuses[sim.primary] = "OB DISCHRG"
    try:
        sim.run_until(WAKE_BUDGET + TIMING.wake_online_timeout + TIMING.desktop_shutdown_wait + POLL)
    finally:
        sim.stop()

    pushed = sim.first("critical_shutdown", "server-ups")
    fsd = sim.first("fsd", "server-ups")
    assert pushed is not None and fsd is not None, sim.events
    assert fsd - pushed >= TIMING.desktop_shutdown_wait - 1, sim.events
