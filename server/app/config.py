import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

STATE_DIR = Path(os.environ.get("UPS_STATE_DIR", "./.runtime-state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

SHARED_TOKEN = os.environ.get("UPS_SHARED_TOKEN", "change-me")
SERVER_PORT = int(os.environ.get("UPS_SERVER_PORT", 8787))

STATE_MAX_AGE = int(os.environ.get("UPS_STATE_MAX_AGE", 300))
UNKNOWN_POLL_INTERVAL = int(os.environ.get("UPS_POLL_INTERVAL", 30))
REQUEST_TIMEOUT_SHORT = int(os.environ.get("UPS_REQUEST_TIMEOUT_SHORT", 5))
REQUEST_TIMEOUT_LONG = int(os.environ.get("UPS_REQUEST_TIMEOUT_LONG", 30))


@dataclass
class SSHConfig:
    host: str
    user: str
    port: int = 22
    key_file: Optional[str] = None


@dataclass
class DesktopConfig:
    agent_url: str
    shutdown_wait: int = 60


@dataclass
class UPSDeviceConfig:
    id: str
    nut_name: str
    local: bool
    onbatt_shutdown_timeout: int
    desktop: DesktopConfig
    ssh: Optional[SSHConfig] = None


def _load_ups_devices() -> list[UPSDeviceConfig]:
    config_path = Path(os.environ.get("UPS_CONFIG_FILE", "ups_config.yml"))

    if not config_path.is_absolute():
        # Try relative to the server/ directory (parent of app/)
        alt = Path(__file__).parent.parent / config_path
        if alt.exists():
            config_path = alt

    if not config_path.exists():
        # Backward-compatible fallback: single device from env vars
        return [UPSDeviceConfig(
            id="main-ups",
            nut_name=os.environ.get("UPS_NUT_NAME", "ups@localhost"),
            local=True,
            onbatt_shutdown_timeout=int(os.environ.get("UPS_ONBATT_SHUTDOWN_TIMEOUT", 600)),
            desktop=DesktopConfig(
                agent_url=os.environ.get("DESKTOP_AGENT_URL", "http://192.168.1.2:8788"),
                shutdown_wait=int(os.environ.get("UPS_DESKTOP_SHUTDOWN_WAIT", 60)),
            ),
        )]

    with open(config_path) as f:
        data = yaml.safe_load(f)

    devices = []
    for d in data.get("ups_devices", []):
        ssh = None
        if d.get("ssh"):
            ssh = SSHConfig(
                host=d["ssh"]["host"],
                user=d["ssh"]["user"],
                port=d["ssh"].get("port", 22),
                key_file=d["ssh"].get("key_file"),
            )
        desktop = DesktopConfig(
            agent_url=d["desktop"]["agent_url"],
            shutdown_wait=d["desktop"].get("shutdown_wait", 60),
        )
        devices.append(UPSDeviceConfig(
            id=d["id"],
            nut_name=d["nut_name"],
            local=d.get("local", False),
            onbatt_shutdown_timeout=d.get("onbatt_shutdown_timeout", 600),
            desktop=desktop,
            ssh=ssh,
        ))
    return devices


UPS_DEVICES: list[UPSDeviceConfig] = _load_ups_devices()
