import sys
import socket
import requests
import logging

# Set up simple logging for the CLI tool
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

try:
    from config import SERVER_BASE, SHARED_TOKEN, UPS_DEVICE_ID
except ImportError:
    # Fallback if config is not in path
    SERVER_BASE = "http://192.168.50.10:8787"
    SHARED_TOKEN = "change-me"
    UPS_DEVICE_ID = "server-ups"

if len(sys.argv) < 2:
    print(f"Usage: {sys.argv[0]} <status>")
    sys.exit(1)

status = sys.argv[1]
hostname = socket.gethostname()

logger.info(f"Sending state '{status}' for {hostname} to {SERVER_BASE}")

try:
    resp = requests.post(
        f"{SERVER_BASE}/api/ups/{UPS_DEVICE_ID}/desktop/update-state",
        headers={"X-UPS-Token": SHARED_TOKEN},
        json={
            "hostname": hostname,
            "status": status,
            "user_active": True,
            "source": "desktop-hook",
        },
        timeout=10,
    )
    resp.raise_for_status()
    logger.info("Successfully updated state")
except Exception as e:
    logger.error(f"Failed to update state: {e}")
    sys.exit(1)