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
    UPSMON_BIN,
    UPSCMD_BIN,
    UPSCMD_USER,
    UPSCMD_PASS,
    SHUTDOWN_FALLBACK_CMD,
    LOGS_DIR,
)
from outage_log import OutageLog
from event_log import (
    EventLog,
    EV_ONBATT, EV_ONLINE, EV_LOWBATT, EV_PHASE_STARTED,
    EV_DESKTOP_NOTIFIED, EV_WOL_SENT, EV_WOL_FAILED,
    EV_SHUTDOWN_PUSHED, EV_DESKTOP_CONFIRMED, EV_DESKTOP_TIMEOUT,
    EV_DESKTOP_OBSERVED, EV_SELF_SHUTDOWN, EV_SERVER_RESTARTED,
    EV_UPSCMD_SENT, EV_UPSCMD_FAILED,
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
        # Set by register_peers() once all contexts exist. Lets an observer find
        # the primary that coordinates the same desktop, so it doesn't cut the
        # desktop's UPS power mid-coordination.
        self._peers: list["UPSContext"] = []
        logs_dir = LOGS_DIR / device.id
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._outage_log = OutageLog(logs_dir)
        self._event_log = EventLog(logs_dir)

    def register_peers(self, contexts):
        """Make the other UPS contexts visible to this one (called at startup)."""
        self._peers = [c for c in contexts if c is not self]

    def _desktop_owner_peers(self) -> list["UPSContext"]:
        """Peer contexts (the primary/-ies) that coordinate this device's desktop.

        Matched by the shared desktop agent URL — a primary inherits its desktop
        config from the observer, so they point at the same desktop.
        """
        if not self.device.desktop:
            return []
        my_url = self.device.desktop.agent_url
        return [
            c for c in self._peers
            if not c.device.is_observer
            and c.device.desktop
            and c.device.desktop.agent_url == my_url
        ]

    @staticmethod
    def _peer_reports_desktop_down(peer: "UPSContext") -> bool:
        """True once the desktop has positively reported it is going away.

        The desktop pushes 'shutting_down'/'offline' only to the primary's ups_id,
        so the observer reads it from the owning primary's desktop state.
        """
        return peer.get_desktop_state().get("status") in ("offline", "shutting_down")

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

    def _power_off_ups(self) -> bool:
        """Send the configured instant shutdown command to this UPS via upscmd.

        Used for observer-role UPSes — the orchestrator does not shut down the
        server, but powers the UPS down cleanly so its battery isn't drained
        deeply while unattended.
        """
        cmd_name = self.device.upscmd.shutdown
        logger.warning(f"[{self.device.id}] Sending upscmd {cmd_name!r} to power off UPS")

        argv = [UPSCMD_BIN]
        if UPSCMD_USER:
            argv += ["-u", UPSCMD_USER]
        if UPSCMD_PASS:
            argv += ["-p", UPSCMD_PASS]
        argv += [self.device.nut_name, cmd_name]

        try:
            result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            logger.error(f"[{self.device.id}] upscmd binary not found at {UPSCMD_BIN!r}")
            self._event_log.record(EV_UPSCMD_FAILED, {"command": cmd_name, "error": "binary_not_found"})
            return False
        except Exception as exc:
            logger.error(f"[{self.device.id}] upscmd failed: {exc}")
            self._event_log.record(EV_UPSCMD_FAILED, {"command": cmd_name, "error": str(exc)})
            return False

        if result.returncode != 0:
            logger.error(
                f"[{self.device.id}] upscmd {cmd_name!r} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
            self._event_log.record(EV_UPSCMD_FAILED, {
                "command": cmd_name,
                "rc": result.returncode,
                "stderr": result.stderr.strip(),
            })
            return False

        self._event_log.record(EV_UPSCMD_SENT, {"command": cmd_name})
        return True

    def _self_shutdown(self):
        """Trigger forced UPS shutdown on the server host via local upsmon."""
        logger.error(f"[{self.device.id}] CRITICAL: triggering upsmon -c fsd")
        self._event_log.record(EV_SELF_SHUTDOWN)
        # Run as root directly (no sudo needed); fall back to sudo if non-root
        if os.geteuid() == 0:
            cmd = [UPSMON_BIN, "-c", "fsd"]
        else:
            cmd = ["sudo", "-n", UPSMON_BIN, "-c", "fsd"]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"[{self.device.id}] upsmon -c fsd failed (rc={result.returncode}): {result.stderr.strip()}")
            if SHUTDOWN_FALLBACK_CMD:
                logger.error(f"[{self.device.id}] Falling back to UPS_SHUTDOWN_FALLBACK_CMD: {SHUTDOWN_FALLBACK_CMD!r}")
                fallback = SHUTDOWN_FALLBACK_CMD.split()
                result = subprocess.run(fallback, check=False, capture_output=True, text=True)
                if result.returncode != 0:
                    logger.error(f"[{self.device.id}] Fallback shutdown also failed (rc={result.returncode}): {result.stderr.strip()}")

    def _terminal_action(self):
        """Final action when the orchestrator must give up: either shut down
        the server (primary role) or power off this UPS (observer role)."""
        if self.device.is_observer:
            self._power_off_ups()
        else:
            self._self_shutdown()

    def _observer_power_off_when_desktop_safe(self):
        """Observer graceful power-off worker.

        An observer powers the *desktop's* UPS. Cutting that power while the
        desktop is still alive (online or merely suspended) yanks the cord on a
        running machine — and races the primary, which may still be about to
        wake the desktop via WoL and shut it down cleanly. So instead of powering
        off the instant our grace timer expires, we hold until the desktop is
        confirmed down by the primary that owns it.

        Backstops so the battery is still protected if the desktop never goes
        down: this UPS hitting low battery, or `observer_poweroff_max_wait`.
        """
        owners = self._desktop_owner_peers()
        if not owners:
            # No primary coordinates this desktop here (e.g. standalone observer
            # or no desktop) — nothing to wait for, power off as before.
            self._power_off_ups()
            return

        max_wait = self.device.timing.observer_poweroff_max_wait
        deadline = now_ts() + max_wait
        logger.info(
            f"[{self.device.id}] Observer: holding UPS power-off until desktop is "
            f"confirmed down (max {max_wait}s)"
        )

        while now_ts() < deadline:
            if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
                logger.info(f"[{self.device.id}] Power restored — aborting UPS power-off")
                return
            if any(self._peer_reports_desktop_down(o) for o in owners):
                logger.info(f"[{self.device.id}] Desktop confirmed down — powering off UPS")
                self._event_log.record(EV_DESKTOP_CONFIRMED, {"source": "primary"})
                break
            status = self.read_ups_status()
            if status and "LB" in status:
                logger.warning(
                    f"[{self.device.id}] UPS low battery while waiting for desktop — "
                    f"powering off UPS now"
                )
                break
            time.sleep(5)
        else:
            logger.warning(
                f"[{self.device.id}] Desktop not confirmed down within {max_wait}s — "
                f"powering off UPS anyway"
            )
            self._event_log.record(EV_DESKTOP_TIMEOUT)

        self._power_off_ups()

    def _wait_for_desktop_then_terminal_action(self):
        wait = self.device.timing.desktop_shutdown_wait
        deadline = now_ts() + wait
        logger.info(f"[{self.device.id}] Waiting up to {wait}s for desktop to shut down")

        while now_ts() < deadline:
            if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
                logger.info(f"[{self.device.id}] Power restored during shutdown wait — aborting")
                return
            state = self.get_desktop_state()
            # NOTE: 'suspended' is deliberately NOT a confirmation here. Both callers
            # reach this only after trying to bring a live/woken desktop down cleanly:
            # the user-prompt path pushes shutdown to an online desktop, and the
            # suspend path first wakes it via WoL. A still-'suspended' status means the
            # desktop never came up to receive the shutdown — treating it as confirmed
            # would collapse the desktop_shutdown_wait window to zero and self-shutdown
            # while the desktop is still asleep on a dying UPS. Wait the full window so
            # it has a real chance to wake and shut down (or time out, then proceed).
            if state.get("status") in ("shutting_down", "offline"):
                logger.info(f"[{self.device.id}] Desktop reported '{state.get('status')}', proceeding with terminal action")
                self._event_log.record(EV_DESKTOP_CONFIRMED, {"status": state.get("status")})
                break
            time.sleep(5)
        else:
            if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
                logger.info(f"[{self.device.id}] Power restored during shutdown wait — aborting")
                return
            logger.warning(f"[{self.device.id}] Desktop shutdown wait timed out, proceeding anyway")
            self._event_log.record(EV_DESKTOP_TIMEOUT)

        self._terminal_action()

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
        if self.device.is_observer:
            # Observer never drives desktop coordination — ignore state changes.
            return
        orch = self.get_orchestrator_state()
        if orch.get("mode") != MODE_MONITORING:
            return

        if new_status in ("offline", "shutting_down"):
            # Desktop is going away on its own. For a primary UPS there's no point
            # keeping the server up; for an observer UPS we should power the UPS
            # off so its battery isn't drained.
            logger.info(f"[{self.device.id}] Desktop reported '{new_status}' during battery monitoring → terminal action")
            with self._action_lock:
                cur = self.get_orchestrator_state()
                if cur.get("mode") == MODE_SHUTTING_DOWN:
                    return
                cur["mode"] = MODE_SHUTTING_DOWN
                self.save_orchestrator_state(cur)
            self._event_log.record(EV_DESKTOP_OBSERVED, {"status": new_status})
            self._outage_log.record_end("shutdown_initiated")
            threading.Thread(target=self._terminal_action, daemon=True).start()
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
        orch = self.get_orchestrator_state()
        onbatt_ts = now_ts()
        orch["mode"] = MODE_MONITORING
        orch["last_event"] = "ONBATT"
        orch["onbatt_since"] = onbatt_ts
        self._outage_log.record_start(onbatt_ts)

        if self.device.is_observer:
            # Observer UPSes never coordinate with the desktop directly — that is
            # the primary's job. An observer only watches its own UPS battery and
            # powers it off (via upscmd) when needed.  Always use OFFLINE_WAIT so
            # the timer fires and `_power_off_ups` is called after the grace period.
            orch["phase"] = PHASE_OFFLINE_WAIT
            orch["phase_deadline"] = now_ts() + timing.desktop_offline_wait
            self._event_log.record(EV_PHASE_STARTED, {"phase": PHASE_OFFLINE_WAIT, "role": "observer"})
            self.save_orchestrator_state(orch)
            remaining = orch["phase_deadline"] - now_ts()
            logger.info(f"[{self.device.id}] Observer: started battery monitoring — upscmd in {remaining}s if still on battery")
            self._arm_deadline_timer(remaining)
            return

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

        if not self.device.desktop or desktop_status not in ("online", "suspended"):
            orch["phase"] = PHASE_OFFLINE_WAIT
            orch["phase_deadline"] = now_ts() + timing.desktop_offline_wait
        elif desktop_status == "suspended":
            orch["phase"] = PHASE_SUSPEND_WAIT
            orch["phase_deadline"] = now_ts() + timing.desktop_suspend_wait
        else:  # online
            orch["phase"] = PHASE_USER_PROMPT
            orch["phase_deadline"] = now_ts() + timing.desktop_online_prompt_wait

        self._event_log.record(EV_PHASE_STARTED, {"phase": orch["phase"], "desktop_state": desktop_status})
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
            self._event_log.record(EV_DESKTOP_NOTIFIED, {"command": "ups_state"})

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
            self._outage_log.record_end("shutdown_initiated")

        if self.device.is_observer:
            # An observer never coordinates the desktop and only ever sits in
            # OFFLINE_WAIT. On a true emergency (low battery) power off at once —
            # the desktop can't be saved anyway. Otherwise hold the UPS power-off
            # until the primary has brought the desktop down cleanly.
            emergency = reason in ("low_battery", "low_battery_event")
            target = self._terminal_action if emergency else self._observer_power_off_when_desktop_safe
            threading.Thread(target=target, daemon=True).start()
            return

        if phase == PHASE_USER_PROMPT:
            threading.Thread(target=self._action_force_shutdown_desktop, daemon=True).start()
        elif phase == PHASE_SUSPEND_WAIT:
            threading.Thread(target=self._action_wake_then_shutdown, daemon=True).start()
        else:  # PHASE_OFFLINE_WAIT
            threading.Thread(target=self._terminal_action, daemon=True).start()

    # -------------------------------------------------------------------------
    # Phase action workers
    # -------------------------------------------------------------------------

    def _action_force_shutdown_desktop(self):
        cmd = self._make_critical_shutdown_command()
        self.save_command(cmd)
        self.push_command_to_desktop(cmd)
        self._event_log.record(EV_SHUTDOWN_PUSHED)
        self._wait_for_desktop_then_terminal_action()

    def _send_wol(self) -> bool:
        """Send a single Wake-on-LAN packet to the desktop. Returns False if there
        is no MAC to target or the send raised."""
        mac = self.device.desktop.mac_address if self.device.desktop else None
        if not mac:
            logger.warning(f"[{self.device.id}] No MAC configured, skipping WoL")
            return False
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
            self._event_log.record(EV_WOL_SENT, {"mac": mac})
            return True
        except Exception as exc:
            logger.error(f"[{self.device.id}] WoL failed: {exc}")
            self._event_log.record(EV_WOL_FAILED, {"mac": mac, "error": str(exc)})
            return False

    def _action_wake_then_shutdown(self):
        self._send_wol()

        # Wait for the desktop to come online so it can shut down gracefully.
        # A single magic packet can be dropped (or land before the NIC is ready),
        # so re-send WoL every wol_retry_interval seconds until the desktop
        # reports online or the wake window expires.
        retry_interval = self.device.timing.wol_retry_interval
        deadline = now_ts() + self.device.timing.wake_online_timeout
        next_retry = now_ts() + retry_interval
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
            if retry_interval > 0 and now_ts() >= next_retry:
                logger.info(f"[{self.device.id}] Desktop still not online, re-sending WoL")
                self._send_wol()
                next_retry = now_ts() + retry_interval
            time.sleep(5)

        if not desktop_came_online:
            logger.warning(f"[{self.device.id}] Desktop did not come online after WoL, pushing shutdown anyway")

        if self.get_orchestrator_state().get("mode") != MODE_SHUTTING_DOWN:
            logger.info(f"[{self.device.id}] Power restored before shutdown push — aborting")
            return

        cmd = self._make_critical_shutdown_command()
        self.save_command(cmd)
        self.push_command_to_desktop(cmd)
        self._event_log.record(EV_SHUTDOWN_PUSHED)

        # Start the shutdown-wait timer only after the command has been pushed.
        # This ensures the desktop has the full desktop_shutdown_wait window to
        # process the command, even if it came online late during the WoL wait.
        self._wait_for_desktop_then_terminal_action()

    # -------------------------------------------------------------------------
    # Reset / startup
    # -------------------------------------------------------------------------

    def _reset_to_idle(self, reason: str):
        logger.info(f"[{self.device.id}] Resetting to idle — reason: {reason}")
        self._outage_log.record_end("power_restored")
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
        self._outage_log.resolve_on_startup()
        self._event_log.record(EV_SERVER_RESTARTED)
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
