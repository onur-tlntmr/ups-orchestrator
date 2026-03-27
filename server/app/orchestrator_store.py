from config import STATE_DIR
from state_store import read_json, write_json, now_ts

ORCHESTRATOR_STATE_FILE = STATE_DIR / "orchestrator_state.json"


def get_orchestrator_state():
    return read_json(
        ORCHESTRATOR_STATE_FILE,
        {
            "mode": "idle",
            "last_event": None,
            "pending_command": None,
            "suspend_deadline": None,
            "updated_at": 0,
        },
    )


def save_orchestrator_state(state):
    state["updated_at"] = now_ts()
    write_json(ORCHESTRATOR_STATE_FILE, state)