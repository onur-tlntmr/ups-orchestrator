import tempfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import state_store


def test_write_and_read_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.json"

        data = {"status": "online", "value": 1}
        state_store.write_json(path, data)

        loaded = state_store.read_json(path, {})
        assert loaded == data


def test_read_json_default_when_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "missing.json"
        loaded = state_store.read_json(path, {"default": True})
        assert loaded == {"default": True}


def test_state_is_fresh_true():
    fresh_state = {"last_seen": state_store.now_ts()}
    assert state_store.state_is_fresh(fresh_state) is True


def test_state_is_fresh_false():
    old_state = {"last_seen": 1}
    assert state_store.state_is_fresh(old_state) is False