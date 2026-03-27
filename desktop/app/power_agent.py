#!/usr/bin/env python3

import os
import socket
import sys
import time
import signal
import logging
import logging.handlers
import requests
from pydbus import SystemBus
from gi.repository import GLib

# Add current directory to path for config import
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from config import (
        SERVER_BASE,
        SHARED_TOKEN,
        REQUEST_TIMEOUT_FAST,
        REQUEST_TIMEOUT_NORMAL,
        NETWORK_WAIT_TIMEOUT,
    )
except ImportError:
    SERVER_BASE = os.environ.get("UPS_SERVER_BASE", "http://192.168.50.10:8787")
    SHARED_TOKEN = os.environ.get("UPS_SHARED_TOKEN", "change-me-secret-token")
    REQUEST_TIMEOUT_FAST = int(os.environ.get("UPS_REQUEST_TIMEOUT_FAST", 2))
    REQUEST_TIMEOUT_NORMAL = int(os.environ.get("UPS_REQUEST_TIMEOUT_NORMAL", 5))
    NETWORK_WAIT_TIMEOUT = int(os.environ.get("UPS_NETWORK_WAIT_TIMEOUT", 5))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[power-agent] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

class PowerAgent:
    def __init__(self):
        try:
            self.bus = SystemBus()
            self.login1 = self.bus.get(
                "org.freedesktop.login1", "/org/freedesktop/login1"
            )
        except Exception as e:
            logger.error(f"Failed to connect to DBus: {e}")
            sys.exit(1)

        self.loop = GLib.MainLoop()
        self.inhibit_fd = None
        self.hostname = socket.gethostname()

    def _send_state(self, state: str, timeout_sec: int = 5, retries: int = 3) -> bool:
        """Sends machine state to the orchestrator server in-process."""
        url = f"{SERVER_BASE}/api/desktop/update-state"
        headers = {"X-UPS-Token": SHARED_TOKEN}
        payload = {
            "hostname": self.hostname,
            "status": state,
            "user_active": True,
            "source": "desktop-power-agent",
        }

        for attempt in range(retries):
            try:
                logger.info(f"Sending state '{state}' to {url} (attempt {attempt + 1})")
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=timeout_sec
                )
                resp.raise_for_status()
                logger.info(f"State '{state}' sent successfully")
                return True
            except Exception as e:
                logger.error(
                    f"Failed to send state '{state}' (attempt {attempt + 1}): {e}"
                )
                if attempt < retries - 1:
                    time.sleep(1)
        return False

    def take_delay_lock(self) -> None:
        if self.inhibit_fd is not None:
            self.release_delay_lock()

        try:
            self.inhibit_fd = self.login1.Inhibit(
                "sleep:shutdown",
                "ups-orchestrator",
                "Run state updates before sleep/shutdown",
                "delay",
            )
            logger.info(f"Delay inhibitor acquired, fd={self.inhibit_fd}")
        except Exception as e:
            logger.error(f"Failed to acquire inhibitor: {e}")

    def release_delay_lock(self) -> None:
        if self.inhibit_fd is not None:
            try:
                os.close(self.inhibit_fd)
                logger.info("Delay inhibitor released")
            except OSError as e:
                logger.warning(f"Failed to close inhibitor fd: {e}")
            finally:
                self.inhibit_fd = None

    def on_prepare_for_sleep(self, sleeping: bool) -> None:
        logger.info(f"PrepareForSleep received: {sleeping}")

        if sleeping:
            self._send_state("suspending", timeout_sec=REQUEST_TIMEOUT_FAST)
            self.release_delay_lock()
        else:
            logger.info("System woke up, waiting for network...")
            time.sleep(NETWORK_WAIT_TIMEOUT)  # Wait for network interface
            self.take_delay_lock()
            self._send_state("online", timeout_sec=REQUEST_TIMEOUT_NORMAL, retries=5)

    def on_prepare_for_shutdown(self, shutting_down: bool) -> None:
        logger.info(f"PrepareForShutdown received: {shutting_down}")

        if shutting_down:
            # We must be quick here
            self._send_state("shutting_down", timeout_sec=REQUEST_TIMEOUT_FAST, retries=1)
            self.release_delay_lock()
        else:
            self.take_delay_lock()

    def handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down agent...")
        self._send_state("offline", timeout_sec=REQUEST_TIMEOUT_FAST, retries=1)
        self.release_delay_lock()
        self.loop.quit()

    def run(self) -> None:
        # Register signal handlers
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

        self.take_delay_lock()
        self._send_state("online", timeout_sec=REQUEST_TIMEOUT_NORMAL)

        self.bus.subscribe(
            iface="org.freedesktop.login1.Manager",
            signal="PrepareForSleep",
            signal_fired=lambda sender,
            obj,
            iface,
            signal,
            params: self.on_prepare_for_sleep(bool(params[0])),
        )

        self.bus.subscribe(
            iface="org.freedesktop.login1.Manager",
            signal="PrepareForShutdown",
            signal_fired=lambda sender,
            obj,
            iface,
            signal,
            params: self.on_prepare_for_shutdown(bool(params[0])),
        )

        logger.info("Power agent started")
        try:
            self.loop.run()
        except Exception as e:
            logger.error(f"Main loop error: {e}")
        finally:
            self.release_delay_lock()


if __name__ == "__main__":
    agent = PowerAgent()
    agent.run()
