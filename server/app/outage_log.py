"""Outage duration history — persisted as a rotating JSON list.

Each entry records a single power outage interval:
  {
    "outage_start":     <unix timestamp>,
    "outage_start_dt":  <ISO 8601>,
    "outage_end":       <unix timestamp | null>,
    "outage_end_dt":    <ISO 8601 | null>,
    "duration_seconds": <int | null>,
    "duration_human":   <str | null>,   e.g. "2m 15s"
    "outcome":          "power_restored" | "power_restored_after_reboot"
                        | "shutdown_initiated" | "unknown" | null
  }

The file is capped at OUTAGE_LOG_MAX_ENTRIES (newest entries kept).
"""

import logging
from pathlib import Path
from typing import Optional

from config import OUTAGE_LOG_MAX_ENTRIES
from state_store import read_json, write_json, now_ts, to_iso

logger = logging.getLogger(__name__)


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    for unit, label in ((3600, "h"), (60, "m"), (1, "s")):
        value, seconds = divmod(seconds, unit)
        if value:
            parts.append(f"{value}{label}")
    return " ".join(parts)


class OutageLog:
    def __init__(self, logs_dir: Path):
        self._path = logs_dir / "outage_history.json"

    def record_start(self, onbatt_ts: int) -> None:
        """Open a new outage entry. Closes any unclosed entry first."""
        entries = self._read()
        if entries and entries[0].get("outage_end") is None:
            duration = onbatt_ts - entries[0]["outage_start"]
            entries[0]["outage_end"] = onbatt_ts
            entries[0]["outage_end_dt"] = to_iso(onbatt_ts)
            entries[0]["duration_seconds"] = duration
            entries[0]["duration_human"] = _human_duration(duration)
            entries[0]["outcome"] = "unknown"
            logger.warning("outage_log: previous entry was unclosed — closed with outcome=unknown")

        entries.insert(0, {
            "outage_start": onbatt_ts,
            "outage_start_dt": to_iso(onbatt_ts),
            "outage_end": None,
            "outage_end_dt": None,
            "duration_seconds": None,
            "duration_human": None,
            "outcome": None,
        })
        self._write(entries)
        logger.info(f"outage_log: outage started at {to_iso(onbatt_ts)}")

    def record_end(self, outcome: str, end_ts: Optional[int] = None) -> None:
        """Close the most recent open outage entry."""
        entries = self._read()
        if not entries or entries[0].get("outage_end") is not None:
            logger.debug("outage_log: record_end called but no open entry")
            return

        ts = end_ts or now_ts()
        duration = ts - entries[0]["outage_start"]
        entries[0]["outage_end"] = ts
        entries[0]["outage_end_dt"] = to_iso(ts)
        entries[0]["duration_seconds"] = duration
        entries[0]["duration_human"] = _human_duration(duration)
        entries[0]["outcome"] = outcome
        self._write(entries)
        logger.info(
            f"outage_log: outage ended — outcome={outcome}, "
            f"duration={_human_duration(duration)} ({duration}s)"
        )

    def resolve_on_startup(self) -> None:
        """Called on server startup. If the last outage ended with a shutdown,
        the server restart time is the real power-restored time. Update the
        entry with the actual offline duration (outage_start → restart_time).
        """
        entries = self._read()
        if not entries or entries[0].get("outcome") != "shutdown_initiated":
            return

        restart_ts = now_ts()
        onbatt_ts = entries[0].get("outage_start")

        if isinstance(onbatt_ts, (int, float)) and onbatt_ts > 0:
            real_duration = int(restart_ts - onbatt_ts)
            entries[0]["duration_seconds"] = real_duration
            entries[0]["duration_human"] = _human_duration(real_duration)
            logger.info(
                f"outage_log: resolved shutdown outage — real offline duration="
                f"{_human_duration(real_duration)} ({real_duration}s)"
            )
        else:
            entries[0]["duration_seconds"] = None
            entries[0]["duration_human"] = "unknown"
            logger.warning("outage_log: outage_start missing or invalid — duration unknown")

        entries[0]["outage_end"] = restart_ts
        entries[0]["outage_end_dt"] = to_iso(restart_ts)
        entries[0]["outcome"] = "power_restored_after_reboot"
        self._write(entries)

    def _read(self) -> list:
        return read_json(self._path, [])

    def _write(self, entries: list) -> None:
        write_json(self._path, entries[:OUTAGE_LOG_MAX_ENTRIES])
