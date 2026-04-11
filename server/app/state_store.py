import json
import time
import uuid
from pathlib import Path

from config import STATE_MAX_AGE


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

    tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def state_is_fresh(state):
    ts = int(state.get("last_seen", 0))
    return ts > 0 and (now_ts() - ts) <= STATE_MAX_AGE
