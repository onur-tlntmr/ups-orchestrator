import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "server" / "scripts" / "upssched-cmd"


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Recorder.requests.append(
            {"path": self.path, "token": self.headers["X-UPS-Token"], "body": json.loads(body)}
        )
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 file")
def test_unreadable_env_file_fails_instead_of_using_default_token(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text("UPS_SHARED_TOKEN=secret\n")
    env_file.chmod(0)

    result = subprocess.run(
        ["bash", str(SCRIPT), "server-ups:ONBATT"],
        env={"PATH": "/usr/bin:/bin", "UPS_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "cannot read" in result.stderr


def test_env_file_from_ups_env_file_is_used(tmp_path):
    _Recorder.requests = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        env_file = tmp_path / "server.env"
        env_file.write_text(
            f"UPS_SERVER_PORT={httpd.server_port}\nUPS_SHARED_TOKEN=from-env-file\n"
        )

        result = subprocess.run(
            ["bash", str(SCRIPT), "server-ups:ONBATT"],
            env={"PATH": "/usr/bin:/bin", "UPS_ENV_FILE": str(env_file)},
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        httpd.shutdown()

    assert result.returncode == 0, result.stderr
    assert _Recorder.requests == [
        {
            "path": "/api/ups/server-ups/event",
            "token": "from-env-file",
            "body": {"event": "ONBATT"},
        }
    ]
