import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import requests
import requests.exceptions

from config import (
    UPSDeviceConfig,
    SHARED_TOKEN,
    REQUEST_TIMEOUT_SHORT,
    REQUEST_TIMEOUT_LONG,
)
from state_store import read_json, write_json, now_ts, state_is_fresh

logger = logging.getLogger(__name__)


class UPSContext:
    def __init__(self, device: UPSDeviceConfig, base_state_dir: Path):
        self.device = device
        self.state_dir = base_state_dir / device.id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._desktop_state_file = self.state_dir / "desktop_state.json"
        self._command_file = self.state_dir / "command.json"
        self._orchestrator_state_file = self.state_dir / "orchestrator_state.json"

    # -------------------------------------------------------------------------
    # State helpers
    # -------------------------------------------------------------------------

    def get_desktop_state(self) -> dict:
        return read_json(self._desktop_state_file, {})

    def save_desktop_state(self, state: dict):
        write_json(self._desktop_state_file, state)

    def get_command(self) -> dict:
        return read_json(self._command_file, {})

    def save_command(self, command: dict):
        write_json(self._command_file, command)

    def get_orchestrator_state(self) -> dict:
        return read_json(self._orchestrator_state_file, {
            "mode": "idle",
            "last_event": None,
            "pending_command": None,
            "suspend_deadline": None,
            "updated_at": 0,
        })

    def save_orchestrator_state(self, state: dict):
        state["updated_at"] = now_ts()
        write_json(self._orchestrator_state_file, state)

    # -------------------------------------------------------------------------
    # UPS status via upsc (local or remote via SSH)
    # -------------------------------------------------------------------------

    def _run_upsc(self, var: str) -> Optional[str]:
        try:
            if self.device.local:
                result = subprocess.run(
                    ["upsc", self.device.nut_name, var],
                    capture_output=True, text=True, timeout=5,
                )
            else:
                ssh = self.device.ssh
                cmd = ["ssh",
                       "-o", "StrictHostKeyChecking=no",
                       "-o", "ConnectTimeout=5",
                       "-p", str(ssh.port),
                       f"{ssh.user}@{ssh.host}"]
                if ssh.key_file:
                    cmd += ["-i", ssh.key_file]
                cmd += ["upsc", self.device.nut_name, var]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            logger.debug(f"[{self.device.id}] upsc/ssh binary not found")
        except Exception as exc:
            logger.debug(f"[{self.device.id}] upsc query for {var!r} failed: {exc}")
        return None

    def read_ups_status(self) -> Optional[str]:
        return self._run_upsc("ups.status")

    def read_ups_battery_charge(self) -> Optional[int]:
        raw = self._run_upsc("battery.charge")
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    # -------------------------------------------------------------------------
    # Desktop communication
    # -------------------------------------------------------------------------

    def fetch_state_from_desktop(self) -> dict:
        try:
            resp = requests.get(
                f"{self.device.desktop.agent_url}/state",
                headers={"X-UPS-Token": SHARED_TOKEN},
                timeout=REQUEST_TIMEOUT_SHORT,
            )
            resp.raise_for_status()
            state = resp.json()
            state["last_seen"] = now_ts()
            self.save_desktop_state(state)
            return state
        except requests.exceptions.RequestException:
            logger.warning(f"[{self.device.id}] Desktop agent unreachable")
        except Exception as exc:
            logger.error(f"[{self.device.id}] fetch_state_from_desktop error: {exc}")
        return {}

    def push_command_to_desktop(self, command: dict) -> bool:
        try:
            logger.info(f"[{self.device.id}] pushing {command['command']} to desktop")
            resp = requests.post(
                f"{self.device.desktop.agent_url}/command",
                headers={"X-UPS-Token": SHARED_TOKEN, "Content-Type": "application/json"},
                json=command,
                timeout=REQUEST_TIMEOUT_LONG,
            )
            resp.raise_for_status()
            logger.info(f"[{self.device.id}] command push successful: {resp.status_code}")
            return True
        except requests.exceptions.RequestException:
            logger.warning(f"[{self.device.id}] Desktop agent unreachable for command push")
        except Exception as exc:
            logger.error(f"[{self.device.id}] push_command error: {exc}")
        return False

    # -------------------------------------------------------------------------
    # Shutdown logic
    # -------------------------------------------------------------------------

    def _self_shutdown(self):
        """Trigger forced UPS shutdown for this device's server."""
        logger.error(f"[{self.device.id}] CRITICAL: triggering upsmon -c fsd")
        if self.device.local:
            subprocess.run(["sudo", "-n", "upsmon", "-c", "fsd"], check=False)
        else:
            ssh = self.device.ssh
            cmd = ["ssh",
                   "-o", "StrictHostKeyChecking=no",
                   "-o", "ConnectTimeout=5",
                   "-p", str(ssh.port),
                   f"{ssh.user}@{ssh.host}"]
            if ssh.key_file:
                cmd += ["-i", ssh.key_file]
            cmd += ["sudo", "-n", "upsmon", "-c", "fsd"]
            try:
                subprocess.run(cmd, timeout=15, check=False)
            except Exception as exc:
                logger.error(f"[{self.device.id}] SSH shutdown failed: {exc}")

    def _wait_for_desktop_then_shutdown(self):
        wait = self.device.desktop.shutdown_wait
        deadline = now_ts() + wait
        logger.info(f"[{self.device.id}] Waiting up to {wait}s for desktop to shut down")

        while now_ts() < deadline:
            state = self.get_desktop_state()
            if state.get("status") in ("shutting_down", "offline", "suspended"):
                logger.info(f"[{self.device.id}] Desktop reported '{state.get('status')}', proceeding with server shutdown")
                break
            time.sleep(5)
        else:
            logger.warning(f"[{self.device.id}] Desktop shutdown wait timed out, proceeding anyway")

        self._self_shutdown()

    def trigger_critical_shutdown(self, reason: str):
        logger.info(f"[{self.device.id}] initiating critical shutdown, reason: {reason}")
        orch = self.get_orchestrator_state()
        orch["mode"] = "awaiting_shutdown"
        self.save_orchestrator_state(orch)

        existing_cmd = self.get_command()
        if (
            existing_cmd
            and existing_cmd.get("command") == "critical_shutdown"
            and existing_cmd.get("status") == "pending"
        ):
            cmd = existing_cmd
        else:
            cmd = {
                "id": f"shutdown-{now_ts()}",
                "command": "critical_shutdown",
                "status": "pending",
                "issued_at": now_ts(),
            }
            self.save_command(cmd)

        state = self.get_desktop_state()
        if state.get("status") == "online":
            logger.info(f"[{self.device.id}] Desktop is online, pushing shutdown command")
            threading.Thread(target=self.push_command_to_desktop, args=(cmd,), daemon=True).start()
        else:
            logger.info(f"[{self.device.id}] Desktop is {state.get('status')}, skipping desktop command")

        threading.Thread(target=self._wait_for_desktop_then_shutdown, daemon=True).start()

    # -------------------------------------------------------------------------
    # UPS status transitions (driven by poll loop)
    # -------------------------------------------------------------------------

    def handle_ups_status_transition(self, ups_status: str):
        orch = self.get_orchestrator_state()
        on_battery = "OB" in ups_status
        low_battery = "LB" in ups_status
        on_line = "OL" in ups_status

        if low_battery:
            if orch.get("mode") != "awaiting_shutdown":
                logger.info(f"[{self.device.id}] Low battery ('{ups_status}'), triggering critical shutdown")
                self.trigger_critical_shutdown("low_battery_direct")
            return

        if on_battery:
            if orch.get("last_event") != "ONBATT":
                logger.info(f"[{self.device.id}] On battery ('{ups_status}'), firing ONBATT")
                orch["last_event"] = "ONBATT"
                if not orch.get("onbatt_since"):
                    orch["onbatt_since"] = now_ts()
                self.save_orchestrator_state(orch)

                state = self.get_desktop_state()
                if state.get("status") == "online":
                    cmd = {
                        "id": f"state-{now_ts()}",
                        "command": "ups_state",
                        "payload": {"event": "ONBATT"},
                        "status": "pending",
                        "issued_at": now_ts(),
                    }
                    self.save_command(cmd)
                    self.push_command_to_desktop(cmd)
            return

        if on_line and orch.get("last_event") == "ONBATT":
            logger.info(f"[{self.device.id}] Back on-line ('{ups_status}'), firing ONLINE")
            orch["mode"] = "idle"
            orch["last_event"] = "ONLINE"
            orch["pending_command"] = None
            orch["suspend_deadline"] = None
            orch["onbatt_since"] = None
            self.save_orchestrator_state(orch)

            cmd = self.get_command()
            if cmd and cmd.get("status") == "pending":
                cmd["status"] = "cancelled"
                cmd["result"] = {"reason": "power_restored"}
                cmd["ack_at"] = now_ts()
                self.save_command(cmd)

    # -------------------------------------------------------------------------
    # Startup
    # -------------------------------------------------------------------------

    def reset_state_on_startup(self):
        logger.info(f"[{self.device.id}] Resetting orchestrator state on startup")
        orch = self.get_orchestrator_state()
        orch["mode"] = "idle"
        orch["last_event"] = "ONLINE"
        orch["onbatt_since"] = None
        orch["suspend_deadline"] = None
        self.save_orchestrator_state(orch)

        cmd = self.get_command()
        if cmd and cmd.get("command") == "critical_shutdown" and cmd.get("status") == "pending":
            cmd["status"] = "cancelled"
            cmd["result"] = {"reason": "server_restart"}
            self.save_command(cmd)
