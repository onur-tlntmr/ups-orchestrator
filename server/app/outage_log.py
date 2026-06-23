"""Outage log — two-file design.

outage_state.json    — mutable; tracks the current in-progress outage (if any)
outage_history.jsonl — append-only; one *completed* outage record per line

Completed record fields:
  {"outage_start": <unix ts>, "outage_start_dt": <ISO 8601>,
   "outage_end": <unix ts>, "outage_end_dt": <ISO 8601>,
   "duration_seconds": <int>, "duration_human": <str>,
   "outcome": "power_restored" | "power_restored_after_reboot"
              | "shutdown_initiated" | "unknown"}
"""

import json
import logging
from pathlib import Path
from typing import Optional

from state_store import now_ts, to_iso

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
        self._state_path = logs_dir / "outage_state.json"
        self._history_path = logs_dir / "outage_history.jsonl"

    def record_start(self, onbatt_ts: int) -> None:
        """Open a new outage. If a previous one was left open, close it first."""
        state = self._read_state()
        if state is not None:
            self._append_completed(state, outcome="unknown", end_ts=onbatt_ts)
            logger.warning("outage_log: previous entry was unclosed — closed with outcome=unknown")

        self._write_state({
            "outage_start": onbatt_ts,
            "outage_start_dt": to_iso(onbatt_ts),
        })
        logger.info(f"outage_log: outage started at {to_iso(onbatt_ts)}")

    def record_end(self, outcome: str, end_ts: Optional[int] = None) -> None:
        """Close the current open outage.

        For shutdown_initiated the state is kept so resolve_on_startup()
        can record the real offline duration after the server restarts.
        All other outcomes are appended to history immediately.
        """
        state = self._read_state()
        if state is None:
            logger.debug("outage_log: record_end called but no open entry")
            return

        ts = end_ts or now_ts()

        if outcome == "shutdown_initiated":
            state["outcome"] = "shutdown_initiated"
            state["shutdown_ts"] = ts
            self._write_state(state)
            logger.info("outage_log: shutdown initiated — will finalize on restart")
        else:
            duration = ts - state["outage_start"]
            self._append_completed(state, outcome=outcome, end_ts=ts)
            self._clear_state()
            logger.info(
                f"outage_log: outage ended — outcome={outcome}, "
                f"duration={_human_duration(duration)} ({duration}s)"
            )

    def resolve_on_startup(self) -> None:
        """Called on server startup.

        If a shutdown was initiated, the restart time is the real end of the
        outage. Append the finalized record to history and clear state.
        """
        state = self._read_state()
        if state is None or state.get("outcome") != "shutdown_initiated":
            return

        restart_ts = now_ts()
        onbatt_ts = state.get("outage_start")

        if isinstance(onbatt_ts, (int, float)) and onbatt_ts > 0:
            real_duration = int(restart_ts - onbatt_ts)
            logger.info(
                f"outage_log: resolved shutdown — real offline duration="
                f"{_human_duration(real_duration)} ({real_duration}s)"
            )
        else:
            logger.warning("outage_log: outage_start missing or invalid — duration unknown")

        self._append_completed(state, outcome="power_restored_after_reboot", end_ts=restart_ts)
        self._clear_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_completed(self, state: dict, outcome: str, end_ts: int) -> None:
        onbatt_ts = state["outage_start"]
        duration = int(end_ts - onbatt_ts)
        entry = {
            "outage_start": onbatt_ts,
            "outage_start_dt": state["outage_start_dt"],
            "outage_end": end_ts,
            "outage_end_dt": to_iso(end_ts),
            "duration_seconds": duration,
            "duration_human": _human_duration(duration),
            "outcome": outcome,
        }
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with self._history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _read_state(self) -> Optional[dict]:
        if not self._state_path.exists():
            return None
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_state(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _clear_state(self) -> None:
        if self._state_path.exists():
            self._state_path.unlink()
