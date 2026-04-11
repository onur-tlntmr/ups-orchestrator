import os
from dataclasses import dataclass, field
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

# Path to ether-wake / etherwake binary (used for Wake-on-LAN).
# If empty, the orchestrator falls back to searching PATH.
ETHER_WAKE_BIN = os.environ.get("UPS_ETHER_WAKE_BIN", "")


@dataclass
class SSHConfig:
    host: str
    user: str
    port: int = 22
    key_file: Optional[str] = None


@dataclass
class TimingConfig:
    # Desktop offline (or unreachable / no desktop) → wait then self-shutdown
    desktop_offline_wait: int = 300          # 5 minutes
    # Desktop online → notify user, wait for response then force shutdown
    desktop_online_prompt_wait: int = 180    # 180 seconds
    # Desktop suspended → wait, then wake & shut down
    desktop_suspend_wait: int = 600          # 10 minutes
    # After issuing desktop shutdown, wait this long for confirmation before self-shutdown
    desktop_shutdown_wait: int = 60
    # After WoL, how long to wait for desktop to report online before pushing shutdown
    wake_online_timeout: int = 60


@dataclass
class DesktopConfig:
    agent_url: str
    mac_address: Optional[str] = None  # required for desktop_suspend_wait → wake flow


@dataclass
class UPSDeviceConfig:
    id: str
    nut_name: str
    local: bool
    timing: TimingConfig = field(default_factory=TimingConfig)
    desktop: Optional[DesktopConfig] = None
    ssh: Optional[SSHConfig] = None


def _parse_timing(d: dict) -> TimingConfig:
    if not d:
        return TimingConfig()
    return TimingConfig(
        desktop_offline_wait=d.get("desktop_offline_wait", TimingConfig.desktop_offline_wait),
        desktop_online_prompt_wait=d.get("desktop_online_prompt_wait", TimingConfig.desktop_online_prompt_wait),
        desktop_suspend_wait=d.get("desktop_suspend_wait", TimingConfig.desktop_suspend_wait),
        desktop_shutdown_wait=d.get("desktop_shutdown_wait", TimingConfig.desktop_shutdown_wait),
        wake_online_timeout=d.get("wake_online_timeout", TimingConfig.wake_online_timeout),
    )


def _load_ups_devices() -> list[UPSDeviceConfig]:
    config_path = Path(os.environ.get("UPS_CONFIG_FILE", "ups_config.yml"))

    if not config_path.is_absolute():
        alt = Path(__file__).parent.parent / config_path
        if alt.exists():
            config_path = alt

    if not config_path.exists():
        # Backward-compatible fallback: single device from env vars
        return [UPSDeviceConfig(
            id="main-ups",
            nut_name=os.environ.get("UPS_NUT_NAME", "ups@localhost"),
            local=True,
            desktop=DesktopConfig(
                agent_url=os.environ.get("DESKTOP_AGENT_URL", "http://192.168.1.2:8788"),
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

        desktop = None
        if d.get("desktop"):
            desktop = DesktopConfig(
                agent_url=d["desktop"]["agent_url"],
                mac_address=d["desktop"].get("mac_address"),
            )

        timing = _parse_timing(d.get("timing"))

        devices.append(UPSDeviceConfig(
            id=d["id"],
            nut_name=d["nut_name"],
            local=d.get("local", False),
            timing=timing,
            desktop=desktop,
            ssh=ssh,
        ))
    return devices


UPS_DEVICES: list[UPSDeviceConfig] = _load_ups_devices()
