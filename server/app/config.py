import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("UPS_STATE_DIR", "./.runtime-state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

DESKTOP_STATE_FILE = STATE_DIR / "desktop_state.json"
COMMAND_FILE = STATE_DIR / "command.json"

SHARED_TOKEN = os.environ.get("UPS_SHARED_TOKEN", "change-me")

DESKTOP_AGENT_URL = os.environ.get("DESKTOP_AGENT_URL", "http://192.168.1.2:8788")

STATE_MAX_AGE = int(os.environ.get("UPS_STATE_MAX_AGE", 300))
UNKNOWN_POLL_INTERVAL = int(os.environ.get("UPS_POLL_INTERVAL", 30))
REQUEST_TIMEOUT_SHORT = int(os.environ.get("UPS_REQUEST_TIMEOUT_SHORT", 5))
REQUEST_TIMEOUT_LONG = int(os.environ.get("UPS_REQUEST_TIMEOUT_LONG", 30))
ONBATT_SHUTDOWN_TIMEOUT = int(os.environ.get("UPS_ONBATT_SHUTDOWN_TIMEOUT", 600))

# NUT UPS name as seen by upsc (e.g. "ups@localhost" or "myups@localhost")
UPS_NUT_NAME = os.environ.get("UPS_NUT_NAME", "ups@localhost")

# How long (seconds) to wait for desktop to confirm shutdown before self-shutting down
DESKTOP_SHUTDOWN_WAIT = int(os.environ.get("UPS_DESKTOP_SHUTDOWN_WAIT", 60))
