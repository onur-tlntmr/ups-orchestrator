"""The power agent's delay lock must really be held by logind.

pydbus returned only the index of Inhibit's fd ('h') reply and dropped the fd,
so logind released the lock at once: the agent logged "Delay inhibitor acquired,
fd=0" while no lock existed, and sleep never waited for the 'suspended' report.

Talks to the real logind, so it needs a system bus and python3-gobject +
python3-pydbus (a desktop: `.venv/bin/python -m pytest tests/`). Skipped in CI.
"""

import importlib.util
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("gi")
pydbus = pytest.importorskip("pydbus")

ROOT = Path(__file__).resolve().parents[1]


def _logind():
    try:
        return pydbus.SystemBus().get("org.freedesktop.login1", "/org/freedesktop/login1")
    except Exception:
        return None


LOGIND = _logind()
pytestmark = pytest.mark.skipif(LOGIND is None, reason="no system bus with logind")


@pytest.fixture
def agent():
    # By path, so desktop/app does not shadow server/app's `config` for other tests.
    spec = importlib.util.spec_from_file_location(
        "power_agent", ROOT / "desktop" / "app" / "power_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = mod.PowerAgent()
    yield a
    a.release_delay_lock()


def _my_locks():
    return [mode for _, who, _, mode, _, pid in LOGIND.ListInhibitors()
            if pid == os.getpid() and who == "ups-orchestrator"]


def _wait_for(cond, timeout=2.0):
    # logind notices a closed lock fd asynchronously.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_delay_lock_is_held_until_released(agent):
    agent.take_delay_lock()

    assert os.readlink(f"/proc/self/fd/{agent.inhibit_fd}").startswith("/run/systemd/inhibit/")
    assert _my_locks() == ["delay"]

    agent.release_delay_lock()

    assert _wait_for(lambda: _my_locks() == [])
