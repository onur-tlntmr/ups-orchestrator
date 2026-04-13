"""Outage event history — persisted as a rotating JSON list.

Each entry records a single power outage:
  {
    "outage_start":      <unix timestamp>,
    "outage_start_dt":   <ISO 8601 datetime string>,
    "outage_end":        <unix timestamp | null>,
    "outage_end_dt":     <ISO 8601 datetime string | null>,
    "duration_seconds":  <int | null>,
    "duration_human":    <str | null>,   e.g. "2m 15s"
    "outcome":           "power_restored" | "shutdown_initiated" | "unknown" | null
  }

The file is capped at OUTAGE_LOG_MAX_ENTRIES (newest entries kept).
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import OUTAGE_LOG_MAX_ENTRIES
from state_store import read_json, write_json, now_ts

logger = logging.getLogger(__name__)


def _to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    def __init__(self, state_dir: Path):
        self._path = state_dir / "outage_history.json"

    # ------------------------------------------------------------------

    def record_start(self, onbatt_ts: int) -> None:
        """Open a new outage entry. Closes any unclosed entry first."""
        entries = self._read()
        if entries and entries[0].get("outage_end") is None:
            duration = onbatt_ts - entries[0]["outage_start"]
            entries[0]["outage_end"] = onbatt_ts
            entries[0]["outage_end_dt"] = _to_iso(onbatt_ts)
            entries[0]["duration_seconds"] = duration
            entries[0]["duration_human"] = _human_duration(duration)
            entries[0]["outcome"] = "unknown"
            logger.warning("outage_log: previous outage entry was unclosed — closed with outcome=unknown")

        entries.insert(0, {
            "outage_start": onbatt_ts,
            "outage_start_dt": _to_iso(onbatt_ts),
            "outage_end": None,
            "outage_end_dt": None,
            "duration_seconds": None,
            "duration_human": None,
            "outcome": None,
        })
        self._write(entries)
        logger.info(f"outage_log: outage started at {_to_iso(onbatt_ts)}")

    def record_end(self, outcome: str, end_ts: Optional[int] = None) -> None:
        """Close the most recent open outage entry."""
        entries = self._read()
        if not entries or entries[0].get("outage_end") is not None:
            logger.debug("outage_log: record_end called but no open entry found")
            return

        ts = end_ts or now_ts()
        duration = ts - entries[0]["outage_start"]
        entries[0]["outage_end"] = ts
        entries[0]["outage_end_dt"] = _to_iso(ts)
        entries[0]["duration_seconds"] = duration
        entries[0]["duration_human"] = _human_duration(duration)
        entries[0]["outcome"] = outcome
        self._write(entries)
        logger.info(
            f"outage_log: outage ended — outcome={outcome}, "
            f"duration={_human_duration(duration)} ({duration}s)"
        )

    # ------------------------------------------------------------------

    def _read(self) -> list:
        return read_json(self._path, [])

    def _write(self, entries: list) -> None:
        write_json(self._path, entries[:OUTAGE_LOG_MAX_ENTRIES])
