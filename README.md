# UPS Orchestrator

An intelligent automation system designed to gracefully manage Linux Desktop and Home Server shutdowns during UPS power events.

## System Architecture

```text
  +------------------+          +-------------------+
  |   Home Server    | <------> |   Desktop Agent   |
  |  (Orchestrator)  |  [Push]  |    (Local Web)    |
  +--------+---------+          +---------+---------+
           |                              |
           | [Internal]                   | [State Report]
           v                              | (online/offline)
  +------------------+                    |
  | Server Hardware  | <------------------+
  +------------------+
```

## Features

- **Push-Based State Model:** The server pushes UPS states (like `ONBATT`) to the desktop agent instantly.
- **Decentralized Decision:** The desktop agent receives the state and decides locally (via user prompt or timeout) whether to sleep or shut down.
- **Continuous Monitoring:** The desktop agent reports its availability (`online`) to the server.
- **Power Recovery Cancellation:** If power returns (`ONLINE`), any pending operations are automatically cancelled on both server and agent.

---

## Operational Flows

### 1. Power Outage Flow (ONBATT)
When power is lost, the server notifies the desktop agent.

```text
Home Server                 Desktop Agent
    |                             |
    | [1] ONBATT Event            |
    |---------------------------->| (Command: ups_state {event: ONBATT})
    |                             |
    |                             | [2] Show UI Prompt (5s)
    |                             | (User Choice: Sleep / Shutdown)
    |                             |
    | [3] Success Ack             |<----------------------------|
    |                             | (Action: suspend / shutdown)
```

### 2. Critical Shutdown Flow (LOWBATT)
When the battery is critical, the server forces an immediate shutdown of both itself and the desktop.

---

## Installation & Configuration

### 1. Server Setup
```bash
# Configuration
cp server/.env.example server/.env
# Edit server/.env with your settings (Token, URLs)
nano server/.env

# Run
python server/app/server.py
```

### 2. Desktop Agent Setup

Runs on your desktop to execute commands and report system state.

```bash
# Install dependencies
pip install -r desktop/requirements.txt

# Configuration
cp desktop/.env.example desktop/.env
# Edit desktop/.env with your settings
nano desktop/.env

# Run
python desktop/app/agent.py
```

---

## Usage & Simulation

### Test Power Outage (ONBATT)
```bash
curl -X POST \
  -H "X-UPS-Token: secret-token" \
  -H "Content-Type: application/json" \
  -d '{"event": "ONBATT"}' \
  http://localhost:8787/api/ups/event
```

### Test Power Restoration (ONLINE)
```bash
curl -X POST \
  -H "X-UPS-Token: secret-token" \
  -H "Content-Type: application/json" \
  -d '{"event": "ONLINE"}' \
  http://localhost:8787/api/ups/event
```

---

## Deployment (systemd)

You can run both the server and the agent as systemd services to ensure they start automatically. The project should be installed in `/opt/ups-orchestrator`.

### 1. Server Service
1. Copy the service file from `server/systemd/ups-orchestrator-server.service` to `/etc/systemd/system/`.
2. Edit the file to set your `User` and verify paths.
3. Run:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now ups-orchestrator-server
   ```

### 2. Desktop Agent Service
1. Copy the service file from `desktop/systemd/ups-orchestrator-agent.service` to `~/.config/systemd/user/`.
2. Edit the file to verify paths.
3. Run:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now ups-orchestrator-agent
   ```
   *Note: Running as a user service ensures the agent has access to your DBUS session for notifications and UI dialogs.*

---

## System Event Hooks (Instant Reporting)

The desktop agent now directly handles suspend and shutdown events via DBus signals, so the separate `ups-agent-hook` script is no longer required.

* No additional symlink or executable hook is needed.
* The `desktop/app/power_agent.py` will send state updates for `suspending`, `online`, and `shutting_down` automatically.

---

---

## SELinux Configuration

If you are using a distribution with SELinux enabled (like Fedora or RHEL), you may need to grant additional permissions.

### 1. Allow Network Ports
By default, SELinux may block the server/agent from binding to non-standard ports.
```bash
# Allow Server Port (8787)
sudo semanage port -a -t http_port_t -p tcp 8787

# Allow Agent Port (8788)
sudo semanage port -a -t http_port_t -p tcp 8788
```

### 2. Allow Execution from /opt
If the project is located in `/opt/ups-orchestrator`, SELinux might block the service execution.
```bash
# Set the correct context for the project directory
sudo chcon -R -t bin_t /opt/ups-orchestrator/
```

### 3. Troubleshooting
Check the audit logs if the service fails to start or communicate:
```bash
sudo ausearch -m AVC -ts recent
```

---

## Automated Testing
```bash
pytest tests/
```

---

## NUT (Network UPS Tools) Integration

Use `server/scripts/upssched-cmd` with `upssched`. Example `upssched.conf` in NUT:
`/etc/ups/upssched.conf` pointing to `/opt/ups-orchestrator/server/scripts/upssched-cmd`.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
© 2026 Onur T.