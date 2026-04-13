import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import requests
import requests.exceptions

import wol
from config import (
    UPSDeviceConfig,
    SHARED_TOKEN,
    REQUEST_TIMEOUT_SHORT,
    REQUEST_TIMEOUT_LONG,
)
from state_store import read_json, write_json, now_ts

logger = logging.getLogger(__name__)


# Orchestrator state machine modes
MODE_IDLE = "idle"
MODE_MONITORING = "monitoring_battery"
MODE_SHUTTING_DOWN = "shutting_down"

# Phases (only meaningful in MODE_MONITORING)
PHASE_USER_PROMPT = "user_prompt"        # desktop online → wait for user response
PHASE_OFFLINE_WAIT = "offline_wait"      # desktop offline / no desktop → just wait
PHASE_SUSPEND_WAIT = "suspend_wait"      # desktop suspended → wait then wake


class UPSContext:
    def __init__(self, device: UPSDeviceConfig, base_state_dir: Path):
        self.device = device
        self.state_dir = base_state_dir / device.id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._desktop_state_file = self.state_dir / "desktop_state.json"
        self._command_file = self.state_dir / "command.json"
        self._orchestrator_state_file = self.state_dir / "orchestrator_state.json"

        self._action_lock = threading.Lock()
        self._deadline_timer: Optional[threading.Timer] = None

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
            "mode": MODE_IDLE,
            "phase": None,
            "phase_deadline": None,
            "onbatt_since": None,
            "last_event": None,
            "updated_at": 0,
        })

    def save_orchestrator_state(self, state: dict):
        state["updated_at"] = now_ts()
        write_json(self._orchestrator_state_file, state)

    # -------------------------------------------------------------------------
    # UPS status via upsc (always local)
    # -------------------------------------------------------------------------

    def _run_upsc(self, var: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["upsc", self.device.nut_name, var],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            logger.debug(f"[{self.device.id}] upsc binary not found")
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
        if not self.device.desktop:
            return {}
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
        if not self.device.desktop:
            return False
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
    # Shutdown primitives
    # -------------------------------------------------------------------------

    def _self_shutdown(self):
        """Trigger forced UPS shutdown on the server host via local upsmon."""
        logger.error(f"[{self.device.id}] CRITICAL: triggering upsmon -c fsd")
        # Run as root directly (no sudo needed); fall back to sudo if non-root
        if os.geteuid() == 0:
            cmd = ["upsmon", "-c", "fsd"]
        else:
            cmd = ["sudo", "-n", "upsmon", "-c", "fsd"]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"[{self.device.id}] upsmon -c fsd failed (rc={result.returncode}): {result.stderr.strip()}")

    def _wait_for_desktop_then_self_shutdown(self):
        wait = self.device.timing.desktop_shutdown_wait
        deadline = now_ts() + wait
        logger.info(f"[{self.device.id}] Waiting up to {wait}s for desktop to shut down")

        while now_ts() < deadline:
            if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
                logger.info(f"[{self.device.id}] Power restored during shutdown wait — aborting")
                return
            state = self.get_desktop_state()
            if state.get("status") in ("shutting_down", "offline", "suspended"):
                logger.info(f"[{self.device.id}] Desktop reported '{state.get('status')}', proceeding with self shutdown")
                break
            time.sleep(5)
        else:
            if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
                logger.info(f"[{self.device.id}] Power restored during shutdown wait — aborting")
                return
            logger.warning(f"[{self.device.id}] Desktop shutdown wait timed out, proceeding anyway")

        self._self_shutdown()

    def _make_critical_shutdown_command(self) -> dict:
        return {
            "id": f"shutdown-{now_ts()}",
            "command": "critical_shutdown",
            "status": "pending",
            "issued_at": now_ts(),
        }

    def _make_ups_state_command(self) -> dict:
        return {
            "id": f"state-{now_ts()}",
            "command": "ups_state",
            "payload": {"event": "ONBATT"},
            "status": "pending",
            "issued_at": now_ts(),
        }

    # -------------------------------------------------------------------------
    # State machine entry points
    # -------------------------------------------------------------------------

    def handle_ups_status_transition(self, ups_status: str):
        on_battery = "OB" in ups_status
        low_battery = "LB" in ups_status
        on_line = "OL" in ups_status

        orch = self.get_orchestrator_state()
        mode = orch.get("mode", MODE_IDLE)

        if low_battery:
            if mode != MODE_SHUTTING_DOWN:
                logger.warning(f"[{self.device.id}] LOW BATTERY ('{ups_status}') — executing immediate action")
                if mode != MODE_MONITORING:
                    self._start_battery_monitoring()
                self._execute_phase_action(reason="low_battery")
            return

        if on_battery:
            if mode == MODE_IDLE:
                self._start_battery_monitoring()
            elif mode == MODE_MONITORING:
                self._check_phase_deadline()
            return

        if on_line:
            if mode != MODE_IDLE:
                self._reset_to_idle("power_restored")
            return

    def check_phase_deadline_if_monitoring(self):
        """Called when upsc is unavailable — still drive the timer forward."""
        orch = self.get_orchestrator_state()
        if orch.get("mode") == MODE_MONITORING:
            self._check_phase_deadline()

    def handle_event(self, event: str):
        """Handle a NUT-pushed event (from upssched-cmd)."""
        logger.info(f"[{self.device.id}] event received: {event}")
        orch = self.get_orchestrator_state()
        mode = orch.get("mode", MODE_IDLE)

        if event == "LOWBATT":
            if mode != MODE_SHUTTING_DOWN:
                if mode != MODE_MONITORING:
                    self._start_battery_monitoring()
                self._execute_phase_action(reason="low_battery_event")

        elif event == "ONBATT":
            if mode == MODE_IDLE:
                self._start_battery_monitoring()

        elif event == "ONLINE":
            if mode != MODE_IDLE:
                self._reset_to_idle("power_restored_event")

        elif event == "desktop_suspend_due":
            if mode == MODE_MONITORING:
                self._execute_phase_action(reason="suspend_due_event")

    def notify_desktop_state_change(self, new_status: str):
        """Called from /update-state when the desktop reports a new state."""
        orch = self.get_orchestrator_state()
        if orch.get("mode") != MODE_MONITORING:
            return

        if new_status in ("offline", "shutting_down"):
            # User shut down successfully → no point keeping the server up
            logger.info(f"[{self.device.id}] Desktop reported '{new_status}' during battery monitoring → self shutdown")
            with self._action_lock:
                cur = self.get_orchestrator_state()
                if cur.get("mode") == MODE_SHUTTING_DOWN:
                    return
                cur["mode"] = MODE_SHUTTING_DOWN
                self.save_orchestrator_state(cur)
            threading.Thread(target=self._self_shutdown, daemon=True).start()
            return

        if new_status == "suspended" and orch.get("phase") == PHASE_USER_PROMPT:
            logger.info(f"[{self.device.id}] Desktop suspended during prompt → switching to suspend_wait phase")
            orch["phase"] = PHASE_SUSPEND_WAIT
            orch["phase_deadline"] = now_ts() + self.device.timing.desktop_suspend_wait
            self.save_orchestrator_state(orch)

    # -------------------------------------------------------------------------
    # Internal state transitions
    # -------------------------------------------------------------------------

    def _start_battery_monitoring(self):
        timing = self.device.timing
        state = self.get_desktop_state()
        desktop_status = state.get("status") if self.device.desktop else None

        # Always do a live fetch when there is a desktop configured.
        # The cached state may be stale (e.g. "suspending" / "online" written just
        # before the desktop went to sleep and the network dropped).
        if self.device.desktop:
            live = self.fetch_state_from_desktop()
            if live:
                desktop_status = live.get("status", desktop_status)
            elif desktop_status not in ("offline", "shutting_down"):
                # Unreachable and last known state was not an explicit offline state →
                # desktop is likely suspended (state update didn't reach us in time).
                desktop_status = "suspended"
                logger.info(f"[{self.device.id}] Desktop unreachable at ONBATT — assuming suspended")

        orch = self.get_orchestrator_state()
        orch["mode"] = MODE_MONITORING
        orch["last_event"] = "ONBATT"
        orch["onbatt_since"] = now_ts()

        if not self.device.desktop or desktop_status not in ("online", "suspended"):
            # No desktop, offline, unknown, or unreachable → just wait
            orch["phase"] = PHASE_OFFLINE_WAIT
            orch["phase_deadline"] = now_ts() + timing.desktop_offline_wait
        elif desktop_status == "suspended":
            orch["phase"] = PHASE_SUSPEND_WAIT
            orch["phase_deadline"] = now_ts() + timing.desktop_suspend_wait
        else:  # online
            orch["phase"] = PHASE_USER_PROMPT
            orch["phase_deadline"] = now_ts() + timing.desktop_online_prompt_wait

        self.save_orchestrator_state(orch)
        remaining = orch["phase_deadline"] - now_ts()
        logger.info(f"[{self.device.id}] Started battery monitoring — phase={orch['phase']}, deadline in {remaining}s")

        # Arm a timer so the deadline fires even if no events arrive.
        self._arm_deadline_timer(remaining)

        # If we entered user_prompt, send the notification immediately
        if orch["phase"] == PHASE_USER_PROMPT:
            cmd = self._make_ups_state_command()
            self.save_command(cmd)
            self.push_command_to_desktop(cmd)

    def _arm_deadline_timer(self, delay: float):
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
        self._deadline_timer = threading.Timer(
            max(delay, 0),
            lambda: self._execute_phase_action(reason="phase_deadline"),
        )
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def _check_phase_deadline(self):
        orch = self.get_orchestrator_state()
        deadline = orch.get("phase_deadline") or 0
        if now_ts() < deadline:
            return
        self._execute_phase_action(reason="phase_deadline")

    def _execute_phase_action(self, reason: str):
        with self._action_lock:
            orch = self.get_orchestrator_state()
            if orch.get("mode") != MODE_MONITORING:
                return  # already shutting down or power restored

            phase = orch.get("phase") or PHASE_OFFLINE_WAIT
            logger.info(f"[{self.device.id}] Executing phase action — phase={phase}, reason={reason}")

            orch["mode"] = MODE_SHUTTING_DOWN
            self.save_orchestrator_state(orch)

        if phase == PHASE_USER_PROMPT:
            threading.Thread(target=self._action_force_shutdown_desktop, daemon=True).start()
        elif phase == PHASE_SUSPEND_WAIT:
            threading.Thread(target=self._action_wake_then_shutdown, daemon=True).start()
        else:  # PHASE_OFFLINE_WAIT
            threading.Thread(target=self._self_shutdown, daemon=True).start()

    # -------------------------------------------------------------------------
    # Phase action workers
    # -------------------------------------------------------------------------

    def _action_force_shutdown_desktop(self):
        cmd = self._make_critical_shutdown_command()
        self.save_command(cmd)
        self.push_command_to_desktop(cmd)
        self._wait_for_desktop_then_self_shutdown()

    def _action_wake_then_shutdown(self):
        mac = self.device.desktop.mac_address if self.device.desktop else None
        if mac:
            try:
                relay = self.device.wol_relay
                if relay:
                    logger.info(f"[{self.device.id}] Sending WoL to {mac} via SSH relay {relay.host!r}")
                    wol.send(mac, relay_ssh=relay.host, relay_identity_file=relay.identity_file or "")
                else:
                    desktop_ip = self.device.desktop.agent_url.split("//")[-1].split(":")[0]
                    iface = wol.iface_for_ip(desktop_ip)
                    logger.info(f"[{self.device.id}] Sending WoL to {mac} via {iface!r}")
                    wol.send(mac, iface=iface)
            except Exception as exc:
                logger.error(f"[{self.device.id}] WoL failed: {exc}")
        else:
            logger.warning(f"[{self.device.id}] No MAC configured, skipping WoL")

        # Wait for desktop to come online so it can shut down gracefully
        deadline = now_ts() + self.device.timing.wake_online_timeout
        desktop_came_online = False
        while now_ts() < deadline:
            if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
                logger.info(f"[{self.device.id}] Power restored during wake wait — aborting shutdown")
                return
            state = self.fetch_state_from_desktop()
            if state.get("status") == "online":
                logger.info(f"[{self.device.id}] Desktop is online after wake, pushing shutdown")
                desktop_came_online = True
                break
            time.sleep(5)

        if not desktop_came_online:
            logger.warning(f"[{self.device.id}] Desktop did not come online after WoL, pushing shutdown anyway")

        if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
            logger.info(f"[{self.device.id}] Power restored before shutdown push — aborting")
            return

        cmd = self._make_critical_shutdown_command()
        self.save_command(cmd)
        self.push_command_to_desktop(cmd)

        # Start the shutdown-wait timer only after the command has been pushed.
        # This ensures the desktop has the full desktop_shutdown_wait window to
        # process the command, even if it came online late during the WoL wait.
        self._wait_for_desktop_then_self_shutdown()

    # -------------------------------------------------------------------------
    # Reset / startup
    # -------------------------------------------------------------------------

    def _reset_to_idle(self, reason: str):
        logger.info(f"[{self.device.id}] Resetting to idle — reason: {reason}")
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None
        orch = self.get_orchestrator_state()
        orch["mode"] = MODE_IDLE
        orch["phase"] = None
        orch["phase_deadline"] = None
        orch["last_event"] = "ONLINE"
        orch["onbatt_since"] = None
        self.save_orchestrator_state(orch)

        cmd = self.get_command()
        if cmd and cmd.get("status") == "pending":
            cmd["status"] = "cancelled"
            cmd["result"] = {"reason": reason}
            cmd["ack_at"] = now_ts()
            self.save_command(cmd)

    def reset_state_on_startup(self):
        logger.info(f"[{self.device.id}] Resetting orchestrator state on startup")
        orch = self.get_orchestrator_state()
        orch["mode"] = MODE_IDLE
        orch["phase"] = None
        orch["phase_deadline"] = None
        orch["last_event"] = "ONLINE"
        orch["onbatt_since"] = None
        self.save_orchestrator_state(orch)

        cmd = self.get_command()
        if cmd and cmd.get("command") == "critical_shutdown" and cmd.get("status") == "pending":
            cmd["status"] = "cancelled"
            cmd["result"] = {"reason": "server_restart"}
            self.save_command(cmd)
