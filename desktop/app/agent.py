import json
import logging
import logging.handlers
import socket
import subprocess
import threading
import time
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from config import (
    SERVER_BASE,
    UPS_DEVICE_ID,
    SHARED_TOKEN,
    AGENT_PORT,
    REQUEST_TIMEOUT_NORMAL,
    SUSPEND_MAX_RETRIES,
    SUSPEND_RETRY_DELAY,
    FORCE_SUSPEND,
)
from ui import prompt_soft_suspend, show_critical_warning

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[agent] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

hostname = socket.gethostname()

HEADERS = {
    "X-UPS-Token": SHARED_TOKEN,
    "Content-Type": "application/json",
}


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


class PreflightStatus(Enum):
    ALLOWED = "allowed"
    REJECTED = "rejected"
    ERROR = "error"


def current_state() -> dict:
    return {
        "hostname": hostname,
        "status": "online",
        "user_active": True,
        "source": "desktop-ui-agent",
    }


def preflight(command_id: str, command_name: str) -> PreflightStatus:
    try:
        resp = requests.post(
            f"{SERVER_BASE}/api/ups/{UPS_DEVICE_ID}/desktop/command/preflight",
            headers=HEADERS,
            json={"id": command_id, "command": command_name},
            timeout=REQUEST_TIMEOUT_NORMAL,
        )
        logger.info(f"Preflight status: {resp.status_code}, body: {resp.text}")
        data = resp.json()
        if bool(data.get("allow", False)):
            return PreflightStatus.ALLOWED
        return PreflightStatus.REJECTED
    except Exception as e:
        logger.error(f"Preflight request failed: {e}")
        return PreflightStatus.ERROR


def ack(command_id: str, status: str, result: dict) -> None:
    try:
        resp = requests.post(
            f"{SERVER_BASE}/api/ups/{UPS_DEVICE_ID}/desktop/command/ack",
            headers=HEADERS,
            json={"id": command_id, "status": status, "result": result},
            timeout=REQUEST_TIMEOUT_NORMAL,
        )
        logger.info(f"Ack status: {resp.status_code}")
    except Exception as e:
        logger.error(f"Ack call failed: {e}")


def do_suspend(command_id: str) -> None:
    print(f"[agent] do_suspend called for {command_id}")

    status = preflight(command_id, "ups_state")

    if status == PreflightStatus.REJECTED:
        print("[agent] preflight rejected for ups_state")
        ack(command_id, "failed", {"reason": "preflight_rejected"})
        return

    if status == PreflightStatus.ERROR:
        print(
            "[agent] preflight ERROR (connection refused). FALLING BACK TO SHUTDOWN for safety!"
        )
        do_shutdown(command_id, fail_safe=True)
        return

    logger.info("Preflight ok, attempting systemctl suspend")

    for attempt in range(1, SUSPEND_MAX_RETRIES + 1):
        logger.info(f"Suspend attempt {attempt}/{SUSPEND_MAX_RETRIES}")
        
        # force suspend (-i) on the last attempt if FORCE_SUSPEND is true
        suspend_cmd = ["systemctl", "suspend"]
        if attempt == SUSPEND_MAX_RETRIES and FORCE_SUSPEND:
            logger.info("This is the final attempt and FORCE_SUSPEND is enabled. Adding -i block.")
            suspend_cmd.append("-i")
            
        code, out = run_cmd(suspend_cmd)

        # Fallback to sudo if regular suspend requires permissions
        if code != 0 and (
            "Access denied" in out or "Interactive authentication required" in out
        ):
            logger.warning(f"{' '.join(suspend_cmd)} returned {code}. Trying with sudo...")
            sudo_cmd = ["sudo", "-n"] + suspend_cmd
            code, out = run_cmd(sudo_cmd)

        if code == 0:
            logger.info("Suspend command executed successfully")
            ack(command_id, "done", {"action": "suspended"})
            return

        logger.error(f"Failed to execute suspend command: {out}")

        if attempt < SUSPEND_MAX_RETRIES:
            logger.info(f"Waiting {SUSPEND_RETRY_DELAY} seconds before retrying...")
            time.sleep(SUSPEND_RETRY_DELAY)

    logger.error("All suspend attempts failed. Falling back to shutdown!")
    do_shutdown(command_id, fail_safe=True)


def do_shutdown(command_id: str, fail_safe: bool = False) -> None:
    if not fail_safe:
        status = preflight(command_id, "critical_shutdown")
        if status == PreflightStatus.REJECTED:
            print("[agent] preflight rejected for critical_shutdown")
            ack(command_id, "failed", {"reason": "preflight_rejected"})
            return
        if status == PreflightStatus.ERROR:
            print(
                "[agent] preflight ERROR during shutdown, but proceeding with shutdown anyway (fail-safe)"
            )

    # Prefer `upsmon -c fsd` so NUT powers off the attached UPS after shutdown
    # (requires POWERDOWNFLAG + ups.target wired up on this host).
    code, out = run_cmd(["sudo", "-n", "upsmon", "-c", "fsd"])
    if code != 0:
        print(
            f"[agent] upsmon -c fsd failed (code={code}). Falling back to systemctl poweroff..."
        )
        code, out = run_cmd(["systemctl", "poweroff"])
    if code != 0:
        print(
            f"[agent] systemctl poweroff failed (code={code}). Falling back to sudo /sbin/shutdown..."
        )
        code, out = run_cmd(["sudo", "-n", "/sbin/shutdown", "-h", "now"])

    print(f"[agent] CRITICAL: system shutdown initiated. code={code}, out={out}")

    if code == 0:
        if not fail_safe:
            ack(command_id, "done", {"action": "shutdown"})
    else:
        if not fail_safe:
            ack(command_id, "failed", {"action": "shutdown", "output": out})


class LocalHandler(BaseHTTPRequestHandler):
    def _json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        token = self.headers.get("X-UPS-Token", "")
        if token != SHARED_TOKEN:
            self._json(403, {"ok": False})
            return

        if self.path == "/state":
            self._json(200, current_state())
            return

        self._json(404, {"ok": False})

    def do_POST(self):
        token = self.headers.get("X-UPS-Token", "")
        if token != SHARED_TOKEN:
            self._json(403, {"ok": False})
            return

        if self.path != "/command":
            self._json(404, {"ok": False})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))

        cmd = payload.get("command")
        cmd_id = payload.get("id")
        delay = int(payload.get("payload", {}).get("delay_seconds", 0))

        logger.info(f"Pushed command received: cmd={cmd!r} id={cmd_id!r} delay={delay}")

        if delay > 0:
            print(f"[agent] sleeping for delayed command: {delay} seconds")
            time.sleep(delay)

        if cmd == "ups_state":
            event = payload.get("payload", {}).get("event")

            if event == "ONBATT":

                def handle():
                    try:
                        print(f"[agent] starting prompt thread for command {cmd_id}")
                        choice = prompt_soft_suspend()
                        print(f"[agent] prompt returned choice={choice!r}")

                        if choice == "sleep":
                            do_suspend(cmd_id)
                        elif choice == "shutdown":
                            logger.info("Executing shutdown action")
                            do_shutdown(cmd_id, fail_safe=True)
                            ack(cmd_id, "done", {"action": "shutdown_by_event"})
                        else:
                            logger.info(f"Command {cmd_id} cancelled by user/logic")
                            ack(cmd_id, "cancelled", {"reason": "user_cancelled"})
                    except Exception as e:
                        logger.error(f"Error in command handling thread: {e}")
                        # If prompt failed, try to shutdown for safety during ONBATT
                        do_shutdown(cmd_id, fail_safe=True)

                threading.Thread(target=handle, daemon=True).start()
                self._json(
                    202,
                    {"ok": True, "message": "state accepted for background processing"},
                )
                return

        if cmd == "critical_shutdown":

            def handle():
                print("[agent] showing critical shutdown warning")
                show_critical_warning()
                do_shutdown(cmd_id)

            threading.Thread(target=handle, daemon=True).start()
            self._json(
                202,
                {"ok": True, "message": "command accepted for background processing"},
            )
            return

        self._json(400, {"ok": False, "reason": "unknown_command"})

    def log_message(self, format, *args):
        return


def local_server():
    print(f"[agent] local state server listening on 0.0.0.0:{AGENT_PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), LocalHandler)
    server.serve_forever()


def push_state():
    try:
        state = current_state()
        print(f"[agent] pushing initial state to server: {state}")
        resp = requests.post(
            f"{SERVER_BASE}/api/ups/{UPS_DEVICE_ID}/desktop/update-state",
            headers=HEADERS,
            json=state,
            timeout=REQUEST_TIMEOUT_NORMAL,
        )
        print(f"[agent] push_state status: {resp.status_code}")
    except Exception as e:
        print(f"[agent] push_state failed: {e}")


if __name__ == "__main__":
    logger.info("Starting agent")
    logger.info(f"SERVER_BASE configured as: {SERVER_BASE}")
    push_state()
    local_server()
