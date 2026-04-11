import logging
import threading
import time

import requests.exceptions
from flask import Flask, jsonify, request, abort

from config import SHARED_TOKEN, UNKNOWN_POLL_INTERVAL, UPS_DEVICES, STATE_DIR, SERVER_PORT
from state_store import now_ts, state_is_fresh
from ups_context import UPSContext

APP = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# Build context map: ups_id -> UPSContext
CONTEXTS: dict[str, UPSContext] = {
    device.id: UPSContext(device, STATE_DIR)
    for device in UPS_DEVICES
}


def require_token():
    if request.headers.get("X-UPS-Token", "") != SHARED_TOKEN:
        abort(403)


def get_context(ups_id: str) -> UPSContext:
    ctx = CONTEXTS.get(ups_id)
    if not ctx:
        abort(404)
    return ctx


# -----------------------------------------------------------------------------
# Poll loop
# -----------------------------------------------------------------------------

def _poll_device(ctx: UPSContext):
    ctx.fetch_state_from_desktop()

    ups_status = ctx.read_ups_status()
    if ups_status:
        ctx.handle_ups_status_transition(ups_status)
    else:
        orch = ctx.get_orchestrator_state()
        if orch.get("last_event") == "ONBATT" and orch.get("onbatt_since"):
            elapsed = now_ts() - orch["onbatt_since"]
            if elapsed >= ctx.device.onbatt_shutdown_timeout:
                logger.info(f"[{ctx.device.id}] {ctx.device.onbatt_shutdown_timeout}s on battery exceeded, shutting down")
                ctx.trigger_critical_shutdown("on_battery_timeout")


def poll_loop():
    while True:
        threads = [
            threading.Thread(target=_poll_device, args=(ctx,), daemon=True)
            for ctx in CONTEXTS.values()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        time.sleep(UNKNOWN_POLL_INTERVAL)


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@APP.get("/health")
def health():
    return {"ok": True}


@APP.get("/api/ups/")
def list_ups():
    require_token()
    return jsonify({
        "ok": True,
        "devices": [
            {"id": d.id, "nut_name": d.nut_name, "local": d.local}
            for d in UPS_DEVICES
        ],
    })


@APP.get("/api/ups/<ups_id>/status")
def ups_status(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    status = ctx.read_ups_status()
    charge = ctx.read_ups_battery_charge()

    if status is None:
        return jsonify({"ok": False, "reason": "upsc_unavailable"}), 503

    return jsonify({
        "ok": True,
        "ups": ctx.device.nut_name,
        "status": status,
        "battery_charge": charge,
        "on_battery": "OB" in status,
        "low_battery": "LB" in status,
    })


@APP.post("/api/ups/<ups_id>/event")
def ups_event(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    payload = request.json
    event = payload.get("event")
    state = ctx.get_desktop_state()
    orch = ctx.get_orchestrator_state()

    logger.info(f"[{ups_id}] ups event received: {event}")

    if event == "LOWBATT":
        ctx.trigger_critical_shutdown("low_battery")

    elif event == "ONBATT":
        orch["last_event"] = "ONBATT"
        if not orch.get("onbatt_since"):
            orch["onbatt_since"] = now_ts()
        ctx.save_orchestrator_state(orch)

        if state.get("status") == "online":
            existing_cmd = ctx.get_command()
            if (
                existing_cmd
                and existing_cmd.get("command") == "ups_state"
                and existing_cmd.get("payload", {}).get("event") == "ONBATT"
                and existing_cmd.get("status") == "pending"
            ):
                cmd = existing_cmd
            else:
                cmd = {
                    "id": f"state-{now_ts()}",
                    "command": "ups_state",
                    "payload": {"event": "ONBATT"},
                    "status": "pending",
                    "issued_at": now_ts(),
                }
                ctx.save_command(cmd)
            ctx.push_command_to_desktop(cmd)
        else:
            logger.info(f"[{ups_id}] Desktop is {state.get('status')}, ignoring ONBATT for desktop")

    elif event == "desktop_suspend_due":
        if orch.get("last_event") == "ONBATT":
            logger.info(f"[{ups_id}] Suspend deadline reached, triggering critical shutdown")
            ctx.trigger_critical_shutdown("desktop_suspend_deadline_reached")
        else:
            logger.info(f"[{ups_id}] Power restored before suspend deadline, ignoring")

    elif event == "ONLINE":
        orch["mode"] = "idle"
        orch["last_event"] = "ONLINE"
        orch["pending_command"] = None
        orch["suspend_deadline"] = None
        orch["onbatt_since"] = None
        ctx.save_orchestrator_state(orch)

        cmd = ctx.get_command()
        if cmd and cmd.get("status") == "pending":
            cmd["status"] = "cancelled"
            cmd["result"] = {"reason": "power_restored"}
            cmd["ack_at"] = now_ts()
            ctx.save_command(cmd)

    return {"ok": True}


@APP.post("/api/ups/<ups_id>/desktop/update-state")
def update_state(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    payload = request.json
    state = {
        "hostname": payload.get("hostname"),
        "status": payload.get("status"),
        "user_active": payload.get("user_active", False),
        "source": payload.get("source"),
        "last_seen": now_ts(),
    }
    ctx.save_desktop_state(state)
    logger.info(f"[{ups_id}] state update: {state['source']} reported {state['status']}")

    orch = ctx.get_orchestrator_state()
    if state["status"] == "online" and orch.get("mode") == "awaiting_shutdown":
        cmd = ctx.get_command()
        if cmd and cmd.get("command") == "critical_shutdown" and cmd.get("status") == "pending":
            logger.info(f"[{ups_id}] Desktop back online while awaiting shutdown, re-pushing command")
            ctx.push_command_to_desktop(cmd)

    return {"ok": True}


@APP.get("/api/ups/<ups_id>/desktop/state")
def desktop_state(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    state = ctx.get_desktop_state()
    if state and state_is_fresh(state):
        return jsonify({"ok": True, "state": state, "source": "cache"})

    fresh = ctx.fetch_state_from_desktop()
    if fresh:
        return jsonify({"ok": True, "state": fresh, "source": "desktop-query"})

    return jsonify({
        "ok": True,
        "state": state or {"hostname": "desktop", "status": "unknown", "user_active": False, "last_seen": 0},
        "source": "unknown",
    })


@APP.get("/api/ups/<ups_id>/desktop/command")
def get_command_api(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    cmd = ctx.get_command()
    if not cmd or cmd.get("status") != "pending":
        return jsonify({"command": None})
    return jsonify({"command": cmd})


@APP.post("/api/ups/<ups_id>/desktop/command")
def create_command(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    payload = request.get_json(force=True)
    command = {
        "id": f"{payload['command']}-{now_ts()}",
        "command": payload["command"],
        "payload": payload.get("payload", {}),
        "issued_at": now_ts(),
        "expires_at": payload.get("expires_at", now_ts() + 900),
        "status": "pending",
    }
    ctx.save_command(command)
    return jsonify({"ok": True, "command": command})


@APP.post("/api/ups/<ups_id>/desktop/command/ack")
def ack(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    payload = request.json
    cmd = ctx.get_command()
    if cmd.get("id") != payload.get("id"):
        return {"ok": False}
    cmd["status"] = payload.get("status")
    ctx.save_command(cmd)
    return {"ok": True}


@APP.post("/api/ups/<ups_id>/desktop/command/preflight")
def preflight(ups_id: str):
    require_token()
    ctx = get_context(ups_id)

    payload = request.get_json(force=True)
    cmd = ctx.get_command()

    if not cmd:
        logger.warning(f"[{ups_id}] preflight failed: no command in state")
        return jsonify({"ok": False, "allow": False, "reason": "no_command"}), 404

    if cmd.get("status") != "pending":
        logger.warning(f"[{ups_id}] preflight failed: command status is {cmd.get('status')}")
        return jsonify({"ok": False, "allow": False, "reason": "command_not_pending"}), 409

    if cmd.get("id") != payload.get("id"):
        logger.warning(f"[{ups_id}] preflight failed: ID mismatch. state={cmd.get('id')} payload={payload.get('id')}")
        return jsonify({"ok": False, "allow": False, "reason": "id_mismatch"}), 409

    if cmd.get("command") != payload.get("command"):
        logger.warning(f"[{ups_id}] preflight failed: command mismatch. state={cmd.get('command')} payload={payload.get('command')}")
        return jsonify({"ok": False, "allow": False, "reason": "command_mismatch"}), 409

    return jsonify({"ok": True, "allow": True})


if __name__ == "__main__":
    for ctx in CONTEXTS.values():
        ctx.reset_state_on_startup()

    threading.Thread(target=poll_loop, daemon=True).start()

    APP.run("0.0.0.0", SERVER_PORT)
