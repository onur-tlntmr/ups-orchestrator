import os
from pathlib import Path

# Load .env if present
env_file = Path(__file__).resolve().parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

SERVER_BASE = os.environ.get("UPS_SERVER_BASE", "http://192.168.50.10:8787")
UPS_DEVICE_ID = os.environ.get("UPS_DEVICE_ID", "server-ups")

SHARED_TOKEN = os.environ.get("UPS_SHARED_TOKEN", "change-me")

AGENT_PORT = int(os.environ.get("UPS_AGENT_PORT", 8788))
UI_PROMPT_TIMEOUT = int(os.environ.get("UPS_UI_PROMPT_TIMEOUT", "5"))
REQUEST_TIMEOUT_FAST = int(os.environ.get("UPS_REQUEST_TIMEOUT_FAST", 2))
REQUEST_TIMEOUT_NORMAL = int(os.environ.get("UPS_REQUEST_TIMEOUT_NORMAL", 5))
NETWORK_WAIT_TIMEOUT = int(os.environ.get("UPS_NETWORK_WAIT_TIMEOUT", 5))

SUSPEND_MAX_RETRIES = int(os.environ.get("UPS_SUSPEND_MAX_RETRIES", 3))
SUSPEND_RETRY_DELAY = int(os.environ.get("UPS_SUSPEND_RETRY_DELAY", 5))

FORCE_SUSPEND = os.environ.get("UPS_FORCE_SUSPEND", "false").lower() == "true"

