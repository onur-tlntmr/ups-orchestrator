import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

TOKEN = "test-token"


class InlineThread:
    """Runs a worker thread's target synchronously on start()."""
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def api(monkeypatch, tmp_path):
    """The Flask app, freshly imported against server/ups_config.example.yml
    (server-ups primary + desktop-ups observer) with state and logs under tmp_path.

    Nothing leaves the process: desktop HTTP, WoL, fsd and upscmd are appended to
    `calls` instead. Worker threads run inline, sleeps return at once and phase
    deadline timers are never armed.

    The fake desktop agent answers each /state fetch with the next entry of
    `desktop_replies` (the last one repeats; {} = unreachable) and obeys a pushed
    critical_shutdown by reporting 'shutting_down'.
    """
    monkeypatch.setenv("UPS_CONFIG_FILE", str(ROOT / "server" / "ups_config.example.yml"))
    monkeypatch.setenv("UPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("UPS_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("UPS_SHARED_TOKEN", TOKEN)
    for mod in ("server", "config", "ups_context"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    import server
    import ups_context

    monkeypatch.setattr(ups_context.threading, "Thread", InlineThread)
    monkeypatch.setattr(ups_context.time, "sleep", lambda s: None)

    h = SimpleNamespace(
        client=server.APP.test_client(),
        headers={"X-UPS-Token": TOKEN},
        primary=server.CONTEXTS["server-ups"],
        calls=[],
        desktop_replies=[{}],
    )

    def fetch(ctx):
        reply = h.desktop_replies.pop(0) if len(h.desktop_replies) > 1 else h.desktop_replies[0]
        h.calls.append(("fetch", reply.get("status")))
        if reply:
            ctx.save_desktop_state(dict(reply))
        return dict(reply)

    def push(ctx, cmd):
        h.calls.append(("push", cmd["command"]))
        if cmd["command"] == "critical_shutdown":
            ctx.save_desktop_state({"status": "shutting_down"})
        return True

    for ctx in server.CONTEXTS.values():
        monkeypatch.setattr(ctx, "fetch_state_from_desktop", lambda c=ctx: fetch(c))
        monkeypatch.setattr(ctx, "push_command_to_desktop", lambda cmd, c=ctx: push(c, cmd))
        monkeypatch.setattr(ctx, "_send_wol", lambda: h.calls.append(("wol",)) or True)
        monkeypatch.setattr(ctx, "_self_shutdown", lambda: h.calls.append(("fsd",)))
        monkeypatch.setattr(ctx, "_power_off_ups", lambda: h.calls.append(("upscmd",)) or True)
        monkeypatch.setattr(ctx, "_arm_deadline_timer", lambda delay: None)

    return h
