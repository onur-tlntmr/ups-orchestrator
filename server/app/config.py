import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

STATE_DIR = Path(os.environ.get("UPS_STATE_DIR", "./.runtime-state"))
LOGS_DIR = Path(os.environ.get("UPS_LOGS_DIR", "./logs"))
TIMEZONE = os.environ.get("UPS_TIMEZONE", "Europe/Istanbul")
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

SHARED_TOKEN = os.environ.get("UPS_SHARED_TOKEN", "change-me")
SERVER_PORT = int(os.environ.get("UPS_SERVER_PORT", 8787))

STATE_MAX_AGE = int(os.environ.get("UPS_STATE_MAX_AGE", 300))
UPSMON_BIN = os.environ.get("UPS_UPSMON_BIN", "upsmon")
OUTAGE_LOG_MAX_ENTRIES = int(os.environ.get("UPS_OUTAGE_LOG_MAX_ENTRIES", 200))
SHUTDOWN_FALLBACK_CMD = os.environ.get("UPS_SHUTDOWN_FALLBACK_CMD", "")
UNKNOWN_POLL_INTERVAL = int(os.environ.get("UPS_POLL_INTERVAL", 30))
REQUEST_TIMEOUT_SHORT = int(os.environ.get("UPS_REQUEST_TIMEOUT_SHORT", 5))
REQUEST_TIMEOUT_LONG = int(os.environ.get("UPS_REQUEST_TIMEOUT_LONG", 30))

UPSCMD_BIN = os.environ.get("UPS_UPSCMD_BIN", "upscmd")
UPSCMD_USER = os.environ.get("UPS_UPSCMD_USER", "")
UPSCMD_PASS = os.environ.get("UPS_UPSCMD_PASS", "")

# UPS roles
ROLE_PRIMARY = "primary"    # this server's UPS — drives server self-shutdown
ROLE_OBSERVER = "observer"  # secondary UPS (e.g. desktop's) — never shuts down server,
                            # uses upscmd instead of upsmon -c fsd


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
class WolRelayConfig:
    host: str                            # e.g. "root@192.168.50.1"
    identity_file: Optional[str] = None  # e.g. "/root/.ssh/wol_key"


@dataclass
class DesktopConfig:
    agent_url: str
    mac_address: Optional[str] = None  # required for desktop_suspend_wait → wake flow


@dataclass
class UpscmdConfig:
    # Instant command name to issue when an observer-mode UPS must power off.
    # Driver-dependent. Common values: "shutdown.return", "shutdown.stayoff",
    # "shutdown.default". Use `upscmd -l <ups>` to list supported commands.
    shutdown: str = "shutdown.return"


@dataclass
class UPSDeviceConfig:
    id: str
    nut_name: str
    role: str = ROLE_PRIMARY
    timing: TimingConfig = field(default_factory=TimingConfig)
    desktop: Optional[DesktopConfig] = None
    wol_relay: Optional[WolRelayConfig] = None
    upscmd: UpscmdConfig = field(default_factory=UpscmdConfig)

    @property
    def is_observer(self) -> bool:
        return self.role == ROLE_OBSERVER


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
            desktop=DesktopConfig(
                agent_url=os.environ.get("DESKTOP_AGENT_URL", "http://192.168.1.2:8788"),
            ),
        )]

    with open(config_path) as f:
        data = yaml.safe_load(f)

    devices = []
    for d in data.get("ups_devices", []):
        desktop = None
        if d.get("desktop"):
            desktop = DesktopConfig(
                agent_url=d["desktop"]["agent_url"],
                mac_address=d["desktop"].get("mac_address"),
            )

        wol_relay = None
        if d.get("wol_relay"):
            wol_relay = WolRelayConfig(
                host=d["wol_relay"]["host"],
                identity_file=d["wol_relay"].get("identity_file"),
            )

        timing = _parse_timing(d.get("timing"))

        upscmd_cfg = UpscmdConfig()
        if d.get("upscmd"):
            upscmd_cfg = UpscmdConfig(
                shutdown=d["upscmd"].get("shutdown", UpscmdConfig.shutdown),
            )

        role = d.get("role", ROLE_PRIMARY)
        if role not in (ROLE_PRIMARY, ROLE_OBSERVER):
            raise ValueError(
                f"Invalid role {role!r} for UPS {d['id']!r}; "
                f"expected one of: {ROLE_PRIMARY!r}, {ROLE_OBSERVER!r}"
            )

        devices.append(UPSDeviceConfig(
            id=d["id"],
            nut_name=d["nut_name"],
            role=role,
            timing=timing,
            desktop=desktop,
            wol_relay=wol_relay,
            upscmd=upscmd_cfg,
        ))

    return devices


UPS_DEVICES: list[UPSDeviceConfig] = _load_ups_devices()
