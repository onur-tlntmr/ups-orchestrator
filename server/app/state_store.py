import json
import time
import uuid
from pathlib import Path

from config import DESKTOP_STATE_FILE, COMMAND_FILE, STATE_MAX_AGE


def now_ts():
    return int(time.time())


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    # Use a unique identifier to prevent race conditions during write
    tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"

    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    finally:
        # Clean up if replace failed for some reason
        if tmp.exists():
            tmp.unlink()


def get_desktop_state():
    return read_json(DESKTOP_STATE_FILE, {})


def save_desktop_state(state):
    write_json(DESKTOP_STATE_FILE, state)


def get_command():
    return read_json(COMMAND_FILE, {})


def save_command(command):
    write_json(COMMAND_FILE, command)


def clear_command():
    write_json(COMMAND_FILE, {})


def state_is_fresh(state):
    ts = int(state.get("last_seen", 0))

    return ts > 0 and (now_ts() - ts) <= STATE_MAX_AGE