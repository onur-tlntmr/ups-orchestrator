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

---

## NUT (Network UPS Tools) Setup

To connect your UPS to the system, you need to install and configure `nut` on the **Home Server**.

### 1. Installation

On Fedora/RHEL:
```bash
sudo dnf install nut
```

On Debian/Ubuntu:
```bash
sudo apt update
sudo apt install nut nut-client nut-server
```

### 2. Basic Configuration

Edit the following files in `/etc/nut/`:

#### `nut.conf`
Sets the operation mode.
```ini
MODE=netserver
```

#### `ups.conf`
Defines your UPS driver and port. Most modern USB UPS units use `usbhid-ups`.
```ini
[mecups]
    driver = nutdrv_qx
    port = auto
    vendorid = 0001
    productid = 0000
    langid_fix = 0x409
    desc = "Server UPS"

```

#### `upsd.conf`
Configures the `upsd` daemon to listen for local connections.
```ini
LISTEN 127.0.0.1 3493
```

#### `upsd.users`
Defines users that can monitor or manage the UPS.
```ini
[upsmon]
    password  = mypass
    upsmon master
```

### 3. Monitoring & Scheduler Configuration

#### `upsmon.conf`
Monitors the UPS and defines the command to run on events.
```ini
MONITOR myups@localhost 1 upsmon mypass master
NOTIFYCMD /sbin/upssched
NOTIFYFLAG ONBATT EXEC+SYSLOG
NOTIFYFLAG ONLINE EXEC+SYSLOG
NOTIFYFLAG LOWBATT EXEC+SYSLOG

# Ensure the UPS powers off after the server shuts down.
POWERDOWNFLAG /etc/killpower
```

#### `upssched.conf`
The scheduler that executes our scripts. You can use the provided template in the project:
```bash
# Link the project's config to /etc/nut/upssched.conf
sudo ln -sf /opt/ups-orchestrator/server/scripts/upssched.conf /etc/nut/upssched.conf
```

### 4. Apply Changes

Restart the services to apply configuration:
```bash
sudo systemctl restart nut-server nut-client
```

Confirm synchronization:
```bash
upsc myups@localhost
```

---

## Desktop NUT Setup

The desktop has its own UPS physically attached and runs NUT independently.
There is **no slave link** between server and desktop — each host is master of
its own UPS only. Cross-machine coordination (user prompt, force shutdown,
WoL) is handled entirely by the orchestrator over HTTP.

### 1. Install NUT on the desktop
```bash
sudo dnf install nut        # Fedora/RHEL
sudo apt install nut        # Debian/Ubuntu
```

### 2. Desktop `ups.conf`
```ini
[desktop-ups]
    driver = usbhid-ups
    port = auto
    desc = "Desktop UPS"
```

### 3. Desktop `upsd.conf` / `upsd.users`
```ini
LISTEN 127.0.0.1 3493
```
```ini
[upsmon]
    password = mypass
    upsmon master
```

### 4. Desktop `upsmon.conf`
The desktop monitors only its own UPS as master. When it reaches critical
state, `upsmon -c fsd` shuts down the machine and — via `POWERDOWNFLAG` — the
UPS powers off afterwards.
```ini
MONITOR desktop-ups@localhost 1 upsmon mypass master

POWERDOWNFLAG /etc/killpower
```

### 5. Allow the desktop agent to run `upsmon -c fsd`
The orchestrator pushes a `critical_shutdown` command over HTTP; the desktop
agent executes `sudo upsmon -c fsd` locally. Add a sudoers entry:
```bash
sudo visudo -f /etc/sudoers.d/ups-orchestrator-agent
```
```
# Adjust the user to match the desktop agent's account
your-user ALL=(root) NOPASSWD: /usr/sbin/upsmon
```

---

## Integration with UPS Orchestrator

The server uses `server/scripts/upssched-cmd` to process events from NUT. Ensure the paths in `/etc/nut/upssched.conf` are correct and the script has execution permissions.

```bash
chmod +x /opt/ups-orchestrator/server/scripts/upssched-cmd
```

---

## Installation & Configuration

### 1. Server Setup
```bash
# Create and activate virtual environment
python -m venv server/.venv
source server/.venv/bin/activate

# Install dependencies
pip install -r server/requirements.txt

# Configuration
cp server/.env.example server/.env
# Edit server/.env with your settings (Token, URLs)
nano server/.env

# Run
python server/app/server.py
```

### 2. Desktop Agent Setup

Runs on your desktop to execute commands and report system state.
The power agent (`power_agent.py`) uses DBus via `pydbus` / GObject
Introspection, which requires system packages **before** creating the venv.

```bash
# Install system dependencies for DBus/GObject (required by power_agent.py)
sudo dnf install python3-gobject    # Fedora/RHEL
sudo apt install python3-gi         # Debian/Ubuntu

# Create venv with access to system site-packages (needed for gi/pydbus)
python -m venv --system-site-packages desktop/.venv
source desktop/.venv/bin/activate

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

## Wake-on-LAN (ether-wake)

The orchestrator can wake a suspended desktop via Wake-on-LAN before forcing a
shutdown (used by the `desktop_suspend_wait` flow). This requires the
`ether-wake` binary on the **server** and a network adapter on the desktop with
WoL enabled.

### 1. Install `ether-wake`

On Fedora/RHEL:
```bash
sudo dnf install net-tools
```

On Debian/Ubuntu:
```bash
sudo apt update
sudo apt install etherwake
```

On Arch:
```bash
sudo pacman -S net-tools
```

> The binary is named `ether-wake` on Fedora/RHEL/Arch and `etherwake` on
> Debian/Ubuntu — the orchestrator looks for both.

### 2. Allow passwordless sudo for the orchestrator user

`ether-wake` needs raw socket access (root). Add a sudoers entry so the
orchestrator can call it without a password:

```bash
sudo visudo -f /etc/sudoers.d/ups-orchestrator-wol
```

```
# Adjust the user and binary path to match your setup
ups-orchestrator ALL=(root) NOPASSWD: /usr/sbin/ether-wake, /usr/sbin/etherwake
```

### 3. Enable WoL on the desktop

Most modern motherboards support WoL but it must be enabled in two places:

**BIOS/UEFI:** Enable *Wake on LAN* / *Power on by PCI-E* / *Resume by PME*.

**Linux desktop NIC:** Enable the `g` (magic packet) flag, e.g.:
```bash
sudo ethtool -s enp4s0 wol g
```

To make it persistent across reboots, create a systemd service or a NetworkManager dispatcher script. On Fedora/Arch with systemd-networkd you can drop a file in `/etc/systemd/network/`:
```ini
[Match]
Name=enp4s0

[Link]
WakeOnLan=magic
```

Verify with:
```bash
sudo ethtool enp4s0 | grep Wake-on
# Wake-on: g
```

### 4. Configure the MAC address

Add the desktop's MAC address to `ups_config.yml`:

```yaml
desktop:
  agent_url: "http://192.168.50.2:8788"
  mac_address: "AA:BB:CC:DD:EE:FF"
```

### 5. Test

From the server, manually verify the magic packet wakes the suspended desktop:
```bash
sudo ether-wake AA:BB:CC:DD:EE:FF
```

> If the desktop is on a different subnet, you'll also need to enable WoL
> forwarding on the router/switch (UDP broadcast on port 9), or use a
> directed broadcast.

---

## SELinux Configuration

If you are using a distribution with SELinux enabled (like Fedora or RHEL), you may need to grant additional permissions.

### 0. Install SELinux Utilities
If `semanage` command is missing, install the required package:
```bash
# On Fedora/RHEL 8+
sudo dnf install policycoreutils-python-utils

# On RHEL 7
sudo yum install policycoreutils-python
```

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

## Firewall Configuration

If you have `firewalld` active, you must allow traffic between the server and the desktop agent.

### 1. Allow Server Port (on Home Server)
Allow the desktop agent to send updates or receive commands on port 8787.
```bash
# Allow specific agent IP
sudo firewall-cmd --permanent --zone=public \
  --add-rich-rule='rule family="ipv4" source address="<AGENT_IP>" port port="8787" protocol="tcp" accept'

sudo firewall-cmd --reload
```

### 2. Allow Agent Port (on Desktop)
Allow the server to poll state or push commands on port 8788.
```bash
# Allow specific server IP
sudo firewall-cmd --permanent --zone=public \
  --add-rich-rule='rule family="ipv4" source address="<SERVER_IP>" port port="8788" protocol="tcp" accept'

sudo firewall-cmd --reload
```

### 3. Verification
Check if the port is actually listening:
```bash
sudo ss -tulpn | grep -E "8787|8788"
```

---

## Automated Testing
```bash
pytest tests/
```

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
© 2026 Onur T.