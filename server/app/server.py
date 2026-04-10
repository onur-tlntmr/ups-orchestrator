import time
import threading
import subprocess
import logging
import requests.exceptions

import requests
from flask import Flask, jsonify, request, abort

from config import (
    SHARED_TOKEN,
    DESKTOP_AGENT_URL,
    UNKNOWN_POLL_INTERVAL,
    REQUEST_TIMEOUT_SHORT,
    REQUEST_TIMEOUT_LONG,
    ONBATT_SHUTDOWN_TIMEOUT,
    UPS_NUT_NAME,
    DESKTOP_SHUTDOWN_WAIT,
)
from state_store import (
    now_ts,
    get_desktop_state,
    save_desktop_state,
    get_command,
    save_command,
    state_is_fresh,
)
from orchestrator_store import get_orchestrator_state, save_orchestrator_state

APP = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Suppress Werkzeug logs for ordinary requests
logging.getLogger("werkzeug").setLevel(logging.ERROR)



def require_token():
    token = request.headers.get("X-UPS-Token", "")

    if token != SHARED_TOKEN:
        abort(403)


def fetch_state_from_desktop():
    try:
        resp = requests.get(
            f"{DESKTOP_AGENT_URL}/state",
            headers={"X-UPS-Token": SHARED_TOKEN},
            timeout=REQUEST_TIMEOUT_SHORT,
        )

        resp.raise_for_status()

        state = resp.json()
        state["last_seen"] = now_ts()
        save_desktop_state(state)
        return state

    except requests.exceptions.RequestException:
        logger.warning("fetch_state_from_desktop failed: Desktop agent unreachable (connection error)")
    except Exception as exc:
        logger.error(f"fetch_state_from_desktop failed with unexpected error: {exc}")
        return {}


def push_command_to_desktop(command):
    try:
        print(
            f"[server] pushing command {command['command']} to {DESKTOP_AGENT_URL}/command"
        )
        resp = requests.post(
            f"{DESKTOP_AGENT_URL}/command",
            headers={"X-UPS-Token": SHARED_TOKEN, "Content-Type": "application/json"},
            json=command,
            timeout=REQUEST_TIMEOUT_LONG,
        )
        resp.raise_for_status()
        logger.info(f"command push successful: {resp.status_code}")
        return True
    except requests.exceptions.RequestException:
        logger.warning("failed to push command to desktop: Desktop agent unreachable (connection error)")
    except Exception as exc:
        logger.error(f"failed to push command with unexpected error: {exc}")
        return False


def read_ups_var(var: str) -> str | None:
    """Read a single variable from the local UPS via upsc."""
    try:
        result = subprocess.run(
            ["upsc", UPS_NUT_NAME, var],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        logger.debug("upsc not found, skipping direct UPS query")
    except Exception as exc:
        logger.debug(f"upsc query for {var!r} failed: {exc}")
    return None


def read_ups_status() -> str | None:
    """Return ups.status string (e.g. 'OL', 'OB', 'OB LB') or None."""
    return read_ups_var("ups.status")


def read_ups_battery_charge() -> int | None:
    """Return battery charge percentage or None."""
    raw = read_ups_var("battery.charge")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _wait_for_desktop_then_shutdown():
    """Background: wait for desktop to confirm shutdown, then self-shutdown."""
    deadline = now_ts() + DESKTOP_SHUTDOWN_WAIT
    logger.info(f"Waiting up to {DESKTOP_SHUTDOWN_WAIT}s for desktop to shut down before self-shutdown")

    while now_ts() < deadline:
        state = get_desktop_state()
        if state.get("status") in ("shutting_down", "offline", "suspended"):
            logger.info(f"Desktop reported '{state.get('status')}', proceeding with server self-shutdown")
            break
        time.sleep(5)
    else:
        logger.warning("Desktop shutdown wait timed out, proceeding with server self-shutdown anyway")

    logger.error("CRITICAL: running upsmon -c fsd for server self-shutdown")
    subprocess.run(["sudo", "-n", "upsmon", "-c", "fsd"], check=False)


def reset_state_on_startup():
    logger.info("resetting orchestrator state on startup to prevent shutdown loops")
    orch = get_orchestrator_state()
    orch["mode"] = "idle"
    orch["last_event"] = "ONLINE"  # Assume online until told otherwise
    orch["onbatt_since"] = None
    orch["suspend_deadline"] = None
    save_orchestrator_state(orch)

    # Also cancel any pending commands if they were for shutdown
    cmd = get_command()
    if (
        cmd
        and cmd.get("command") == "critical_shutdown"
        and cmd.get("status") == "pending"
    ):
        cmd["status"] = "cancelled"
        cmd["result"] = {"reason": "server_restart"}
        save_command(cmd)


def trigger_critical_shutdown(reason):
    logger.info(f"initiating critical shutdown, reason: {reason}")
    orch = get_orchestrator_state()
    orch["mode"] = "awaiting_shutdown"
    save_orchestrator_state(orch)

    # Create shutdown command
    existing_cmd = get_command()
    if (
        existing_cmd
        and existing_cmd.get("command") == "critical_shutdown"
        and existing_cmd.get("status") == "pending"
    ):
        logger.info("critical_shutdown command is already pending")
        cmd = existing_cmd
    else:
        cmd = {
            "id": f"shutdown-{now_ts()}",
            "command": "critical_shutdown",
            "status": "pending",
            "issued_at": now_ts(),
        }
        save_command(cmd)

    state = get_desktop_state()
    if state.get("status") in ["online"]:
        logger.info("desktop is online, pushing shutdown command asynchronously")
        threading.Thread(
            target=push_command_to_desktop, args=(cmd,), daemon=True
        ).start()
    else:
        logger.info(
            f"desktop is {state.get('status')}, skipping desktop shutdown command"
        )

    threading.Thread(target=_wait_for_desktop_then_shutdown, daemon=True).start()


def _handle_ups_status_transition(ups_status: str):
    """Derive UPS events from upsc status string and drive orchestrator state."""
    orch = get_orchestrator_state()
    on_battery = "OB" in ups_status
    low_battery = "LB" in ups_status
    on_line = "OL" in ups_status

    if low_battery:
        if orch.get("mode") != "awaiting_shutdown":
            logger.info(f"upsc reports low battery ('{ups_status}'), triggering critical shutdown")
            trigger_critical_shutdown("low_battery_direct")
        return

    if on_battery:
        if orch.get("last_event") != "ONBATT":
            logger.info(f"upsc reports on-battery ('{ups_status}'), firing ONBATT")
            orch["last_event"] = "ONBATT"
            if not orch.get("onbatt_since"):
                orch["onbatt_since"] = now_ts()
            save_orchestrator_state(orch)

            state = get_desktop_state()
            if state.get("status") in ["online"]:
                cmd = {
                    "id": f"state-{now_ts()}",
                    "command": "ups_state",
                    "payload": {"event": "ONBATT"},
                    "status": "pending",
                    "issued_at": now_ts(),
                }
                save_command(cmd)
                push_command_to_desktop(cmd)
        return

    if on_line and orch.get("last_event") == "ONBATT":
        logger.info(f"upsc reports back on-line ('{ups_status}'), firing ONLINE")
        orch["mode"] = "idle"
        orch["last_event"] = "ONLINE"
        orch["pending_command"] = None
        orch["suspend_deadline"] = None
        orch["onbatt_since"] = None
        save_orchestrator_state(orch)

        cmd = get_command()
        if cmd and cmd.get("status") == "pending":
            cmd["status"] = "cancelled"
            cmd["result"] = {"reason": "power_restored"}
            cmd["ack_at"] = now_ts()
            save_command(cmd)


def poll_loop():
    while True:
        fetch_state_from_desktop()

        # Direct UPS monitoring via upsc (USB is on this server)
        ups_status = read_ups_status()
        if ups_status:
            _handle_ups_status_transition(ups_status)
        else:
            # upsc unavailable — fall back to timeout-based shutdown if on battery
            orch = get_orchestrator_state()
            if orch.get("last_event") == "ONBATT" and orch.get("onbatt_since"):
                if now_ts() - orch.get("onbatt_since") >= ONBATT_SHUTDOWN_TIMEOUT:
                    logger.info(f"{ONBATT_SHUTDOWN_TIMEOUT}s on battery exceeded, shutting down.")
                    trigger_critical_shutdown("on_battery_timeout")

        time.sleep(UNKNOWN_POLL_INTERVAL)


@APP.get("/health")
def health():
    return {"ok": True}


@APP.post("/api/desktop/update-state")
def update_state():
    require_token()

    payload = request.json

    state = {
        "hostname": payload.get("hostname"),
        "status": payload.get("status"),
        "user_active": payload.get("user_active", False),
        "source": payload.get("source"),
        "last_seen": now_ts(),
    }

    save_desktop_state(state)
    logger.info(f"state update: {state['source']} reported {state['status']}")

    # Logic: if desktop is "ready" and orchestrator is awaiting shutdown, send the command.
    # This handles the case where the desktop was asleep and we just woke it up.
    orch = get_orchestrator_state()
    if state["status"] == "online" and orch.get("mode") == "awaiting_shutdown":
        cmd = get_command()
        if (
            cmd
            and cmd.get("command") == "critical_shutdown"
            and cmd.get("status") == "pending"
        ):
            logger.info("desktop is ready, pushing critical_shutdown")
            push_command_to_desktop(cmd)

    return {"ok": True}


@APP.get("/api/desktop/state")
def desktop_state():
    require_token()

    state = get_desktop_state()
    if state and state_is_fresh(state):
        return jsonify({"ok": True, "state": state, "source": "cache"})

    fresh = fetch_state_from_desktop()
    if fresh:
        return jsonify({"ok": True, "state": fresh, "source": "desktop-query"})

    return jsonify(
        {
            "ok": True,
            "state": state
            or {
                "hostname": "desktop",
                "status": "unknown",
                "user_active": False,
                "last_seen": 0,
            },
            "source": "unknown",
        }
    )


@APP.get("/api/desktop/command")
def get_command_api():
    require_token()

    cmd = get_command()

    if not cmd:
        return jsonify({"command": None})

    if cmd.get("status") != "pending":
        return jsonify({"command": None})

    return jsonify({"command": cmd})


@APP.post("/api/desktop/command")
def create_command():
    require_token()

    payload = request.get_json(force=True)

    command = {
        "id": f"{payload['command']}-{now_ts()}",
        "command": payload["command"],
        "payload": payload.get("payload", {}),
        "issued_at": now_ts(),
        "expires_at": payload.get("expires_at", now_ts() + 900),
        "status": "pending",
    }

    save_command(command)

    return jsonify({"ok": True, "command": command})


@APP.post("/api/desktop/command/ack")
def ack():
    require_token()

    payload = request.json

    cmd = get_command()

    if cmd.get("id") != payload.get("id"):
        return {"ok": False}

    cmd["status"] = payload.get("status")

    save_command(cmd)

    return {"ok": True}


@APP.post("/api/desktop/command/preflight")
def preflight():
    require_token()

    payload = request.get_json(force=True)

    cmd = get_command()

    if not cmd:
        logger.warning("preflight failed: no command in state")
        return jsonify(
            {
                "ok": False,
                "allow": False,
                "reason": "no_command",
            }
        ), 404

    if cmd.get("status") != "pending":
        logger.warning(f"preflight failed: command status is {cmd.get('status')}")
        return jsonify(
            {
                "ok": False,
                "allow": False,
                "reason": "command_not_pending",
            }
        ), 409

    if cmd.get("id") != payload.get("id"):
        logger.warning(
            f"preflight failed: ID mismatch. state={cmd.get('id')} payload={payload.get('id')}"
        )
        return jsonify(
            {
                "ok": False,
                "allow": False,
                "reason": "id_mismatch",
            }
        ), 409

    if cmd.get("command") != payload.get("command"):
        logger.warning(
            f"preflight failed: command mismatch. state={cmd.get('command')} payload={payload.get('command')}"
        )
        return jsonify(
            {
                "ok": False,
                "allow": False,
                "reason": "command_mismatch",
            }
        ), 409

    return jsonify(
        {
            "ok": True,
            "allow": True,
        }
    )


@APP.get("/api/ups/status")
def ups_status():
    require_token()

    status = read_ups_status()
    charge = read_ups_battery_charge()

    if status is None:
        return jsonify({"ok": False, "reason": "upsc_unavailable"}), 503

    return jsonify({
        "ok": True,
        "ups": UPS_NUT_NAME,
        "status": status,
        "battery_charge": charge,
        "on_battery": "OB" in status,
        "low_battery": "LB" in status,
    })


@APP.post("/api/ups/event")
def ups_event():
    require_token()

    payload = request.json
    event = payload.get("event")
    state = get_desktop_state()
    orch = get_orchestrator_state()

    logger.info(f"ups event received: {event}")

    if event == "LOWBATT":
        trigger_critical_shutdown("low_battery")

    elif event == "ONBATT":
        logger.info("power outage detected (ONBATT)")
        orch["last_event"] = "ONBATT"
        if not orch.get("onbatt_since"):
            orch["onbatt_since"] = now_ts()
        save_orchestrator_state(orch)

        # Requirement: Power outage detected. Only notify if desktop is actually active.
        if state.get("status") in ["online"]:
            logger.info("desktop is online, sending ups_state command")
            existing_cmd = get_command()
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
                save_command(cmd)

            push_command_to_desktop(cmd)
        else:
            logger.info(
                f"desktop is {state.get('status')}, ignoring power outage event for desktop"
            )

    elif event == "desktop_suspend_due":
        logger.info("desktop suspend deadline reached (desktop_suspend_due)")
        # If desktop is still on battery, it's time to force shutdown or suspend
        if orch.get("last_event") == "ONBATT":
            logger.info("desktop still on battery, triggering critical shutdown")
            trigger_critical_shutdown("desktop_suspend_deadline_reached")
        else:
            logger.info("power restored but timer fired, ignoring")

    elif event == "ONLINE":
        logger.info("power restored, resetting state")
        orch["mode"] = "idle"
        orch["last_event"] = "ONLINE"
        orch["pending_command"] = None
        orch["suspend_deadline"] = None
        orch["onbatt_since"] = None
        save_orchestrator_state(orch)

        # Cancel any pending commands
        cmd = get_command()
        if cmd and cmd.get("status") == "pending":
            cmd["status"] = "cancelled"
            cmd["result"] = {"reason": "power_restored"}
            cmd["ack_at"] = now_ts()
            save_command(cmd)

        return jsonify({"ok": True, "event": "ONLINE"})

    return {"ok": True}


if __name__ == "__main__":
    reset_state_on_startup()

    threading.Thread(target=poll_loop, daemon=True).start()

    APP.run("0.0.0.0", 8787)
