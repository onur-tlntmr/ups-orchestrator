"""Action event log — persisted as a rotating flat JSON list.

Each entry records a single action taken by the orchestrator:
  {
    "time":     <unix timestamp>,
    "time_dt":  <ISO 8601>,
    "event":    <str>,   see EVENT_* constants below
    "detail":   <dict | null>
  }

Events are appended newest-first. The file is capped at
OUTAGE_LOG_MAX_ENTRIES entries.
"""

import logging
from pathlib import Path
from typing import Optional

from config import OUTAGE_LOG_MAX_ENTRIES
from state_store import read_json, write_json, now_ts, to_iso

logger = logging.getLogger(__name__)

# Event name constants
EV_ONBATT           = "onbatt_detected"
EV_ONLINE           = "power_restored"
EV_LOWBATT          = "lowbatt_detected"
EV_PHASE_STARTED    = "phase_started"
EV_DESKTOP_NOTIFIED = "desktop_notified"
EV_WOL_SENT         = "wol_sent"
EV_WOL_FAILED       = "wol_failed"
EV_SHUTDOWN_PUSHED  = "critical_shutdown_pushed"
EV_DESKTOP_CONFIRMED = "desktop_shutdown_confirmed"
EV_DESKTOP_TIMEOUT  = "desktop_shutdown_timeout"
EV_DESKTOP_OBSERVED = "desktop_shutdown_observed"
EV_SELF_SHUTDOWN    = "server_self_shutdown"
EV_SERVER_RESTARTED = "server_restarted"


class EventLog:
    def __init__(self, logs_dir: Path):
        self._path = logs_dir / "event_log.json"

    def record(self, event: str, detail: Optional[dict] = None) -> None:
        ts = now_ts()
        entry = {
            "time": ts,
            "time_dt": to_iso(ts),
            "event": event,
            "detail": detail,
        }
        entries = self._read()
        entries.insert(0, entry)
        self._write(entries)
        logger.debug(f"event_log: {event} {detail or ''}")

    def _read(self) -> list:
        return read_json(self._path, [])

    def _write(self, entries: list) -> None:
        write_json(self._path, entries[:OUTAGE_LOG_MAX_ENTRIES])
