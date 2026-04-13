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


def _execute_shutdown() -> tuple[int, str]:
    """Try shutdown methods in order: upsmon fsd → systemctl poweroff → sudo shutdown.

    Returns (returncode, output) of the first method that succeeds,
    or the last failure if all methods fail.
    """
    # NUT 2.8+ requires -P <pid> for upsmon -c fsd
    _, pid_out = run_cmd(["pidof", "upsmon"])
    pid = pid_out.strip().split()[0] if pid_out.strip() else ""
    upsmon_cmd = ["sudo", "-n", "/usr/bin/upsmon", "-c", "fsd"]
    if pid:
        upsmon_cmd += ["-P", pid]
    code, out = run_cmd(upsmon_cmd)
    if code == 0:
        logger.info(f"Shutdown initiated via upsmon -c fsd (pid={pid or 'unknown'})")
        return code, out

    logger.warning(f"upsmon -c fsd failed (code={code}, out={out!r}). Falling back to systemctl poweroff...")

    code, out = run_cmd(["systemctl", "poweroff"])
    if code == 0:
        logger.info("Shutdown initiated via systemctl poweroff")
        return code, out

    logger.warning(f"systemctl poweroff failed (code={code}, out={out!r}). Falling back to sudo /sbin/shutdown...")

    code, out = run_cmd(["sudo", "-n", "/sbin/shutdown", "-h", "now"])
    if code == 0:
        logger.info("Shutdown initiated via sudo /sbin/shutdown -h now")
    else:
        logger.error(f"sudo /sbin/shutdown also failed (code={code}, out={out!r})")

    return code, out


def do_suspend(command_id: str) -> None:
    logger.info(f"do_suspend called for {command_id}")

    status = preflight(command_id, "ups_state")

    if status == PreflightStatus.REJECTED:
        logger.warning("Preflight rejected for ups_state")
        ack(command_id, "failed", {"reason": "preflight_rejected"})
        return

    if status == PreflightStatus.ERROR:
        logger.error("Preflight ERROR (connection refused). Falling back to shutdown for safety!")
        do_shutdown(command_id, fail_safe=True)
        return

    logger.info("Preflight ok, attempting systemctl suspend")

    for attempt in range(1, SUSPEND_MAX_RETRIES + 1):
        logger.info(f"Suspend attempt {attempt}/{SUSPEND_MAX_RETRIES}")

        suspend_cmd = ["systemctl", "suspend"]
        if attempt == SUSPEND_MAX_RETRIES and FORCE_SUSPEND:
            logger.info("Final attempt with FORCE_SUSPEND enabled. Adding -i flag.")
            suspend_cmd.append("-i")

        code, out = run_cmd(suspend_cmd)

        if code != 0 and ("Access denied" in out or "Interactive authentication required" in out):
            logger.warning(f"{' '.join(suspend_cmd)} returned {code}. Trying with sudo...")
            code, out = run_cmd(["sudo", "-n"] + suspend_cmd)

        if code == 0:
            logger.info("Suspend command executed successfully")
            ack(command_id, "done", {"action": "suspended"})
            return

        logger.error(f"Suspend failed: {out}")

        if attempt < SUSPEND_MAX_RETRIES:
            logger.info(f"Waiting {SUSPEND_RETRY_DELAY}s before retrying...")
            time.sleep(SUSPEND_RETRY_DELAY)

    logger.error("All suspend attempts failed. Falling back to shutdown!")
    do_shutdown(command_id, fail_safe=True)


def do_shutdown(command_id: str, fail_safe: bool = False) -> None:
    if not fail_safe:
        status = preflight(command_id, "critical_shutdown")
        if status == PreflightStatus.REJECTED:
            logger.warning("Preflight rejected for critical_shutdown")
            ack(command_id, "failed", {"reason": "preflight_rejected"})
            return
        if status == PreflightStatus.ERROR:
            logger.warning("Preflight ERROR during shutdown, proceeding anyway")

    code, out = _execute_shutdown()
    logger.info(f"Shutdown result: code={code}, out={out!r}")

    if not fail_safe:
        if code == 0:
            ack(command_id, "done", {"action": "shutdown"})
        else:
            ack(command_id, "failed", {"action": "shutdown", "output": out})


class LocalHandler(BaseHTTPRequestHandler):
    def _json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.headers.get("X-UPS-Token", "") != SHARED_TOKEN:
            self._json(403, {"ok": False})
            return
        if self.path == "/state":
            self._json(200, current_state())
            return
        self._json(404, {"ok": False})

    def do_POST(self):
        if self.headers.get("X-UPS-Token", "") != SHARED_TOKEN:
            self._json(403, {"ok": False})
            return
        if self.path != "/command":
            self._json(404, {"ok": False})
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))

        cmd = payload.get("command")
        cmd_id = payload.get("id")
        delay = int(payload.get("payload", {}).get("delay_seconds", 0))

        logger.info(f"Pushed command received: cmd={cmd!r} id={cmd_id!r} delay={delay}")

        if cmd == "ups_state":
            event = payload.get("payload", {}).get("event")
            if event == "ONBATT":
                def handle():
                    if delay > 0:
                        logger.info(f"Sleeping {delay}s before handling command")
                        time.sleep(delay)
                    try:
                        logger.info(f"Starting prompt for command {cmd_id}")
                        choice = prompt_soft_suspend()
                        logger.info(f"Prompt returned choice={choice!r}")
                        if choice == "sleep":
                            do_suspend(cmd_id)
                        elif choice == "shutdown":
                            logger.info("Executing shutdown action")
                            # fail_safe=False: preflight checks if command still valid
                            # (power may have been restored while dialog was open)
                            do_shutdown(cmd_id, fail_safe=False)
                        else:
                            logger.info(f"Command {cmd_id} cancelled")
                            ack(cmd_id, "cancelled", {"reason": "user_cancelled"})
                    except Exception as e:
                        logger.error(f"Error in command handling thread: {e}")
                        do_shutdown(cmd_id, fail_safe=True)

                threading.Thread(target=handle, daemon=True).start()
                self._json(202, {"ok": True, "message": "state accepted for background processing"})
                return

        if cmd == "critical_shutdown":
            def handle():
                if delay > 0:
                    logger.info(f"Sleeping {delay}s before shutdown")
                    time.sleep(delay)
                logger.info("Showing critical shutdown warning")
                threading.Thread(target=show_critical_warning, daemon=True).start()
                do_shutdown(cmd_id, fail_safe=True)

            threading.Thread(target=handle, daemon=True).start()
            self._json(202, {"ok": True, "message": "command accepted for background processing"})
            return

        self._json(400, {"ok": False, "reason": "unknown_command"})

    def log_message(self, format, *args):
        return


def local_server():
    logger.info(f"Local state server listening on 0.0.0.0:{AGENT_PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), LocalHandler)
    server.serve_forever()


def push_state():
    try:
        state = current_state()
        logger.info(f"Pushing initial state to server: {state}")
        resp = requests.post(
            f"{SERVER_BASE}/api/ups/{UPS_DEVICE_ID}/desktop/update-state",
            headers=HEADERS,
            json=state,
            timeout=REQUEST_TIMEOUT_NORMAL,
        )
        logger.info(f"Push state status: {resp.status_code}")
    except Exception as e:
        logger.error(f"Push state failed: {e}")


if __name__ == "__main__":
    logger.info("Starting agent")
    logger.info(f"SERVER_BASE configured as: {SERVER_BASE}")
    push_state()
    local_server()
