# UPS Orchestrator

An intelligent automation system designed to gracefully manage Linux Desktop and Home Server shutdowns during UPS power events.

## System Architecture

```text
  +-----------------+ USB +------------------+ USB +------------------+
  |   Server UPS    |<----|   Home Server    |---->|   Desktop UPS    |
  |   (primary)     |     |  (Orchestrator,  |     |    (observer)    |
  +-----------------+     |    NUT master    |     +--------+---------+
                          |   for both UPS)  |              |
                          +--------+---------+              | (powers)
                                   |                        v
                                   |  HTTP push     +------------------+
                                   +--------------->|   Desktop Agent  |
                                       (state,      |    (DBus, UI)    |
                                        commands)   +------------------+
```

The server is the single point of UPS control. Both UPSes are USB-attached
to the server, so the orchestrator can shut down the desktop's UPS even when
the desktop itself is already powered off (e.g. after a previous outage).

* **Primary** UPS (`role: primary`): drives `upsmon -c fsd` to shut the
  server down on LOWBATT.
* **Observer** UPS (`role: observer`): never shuts the server down. On
  LOWBATT or after the configured offline-wait timer, the orchestrator
  issues `upscmd shutdown.return` so the UPS powers off cleanly,
  protecting its battery from deep discharge.

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
Defines your UPS drivers. Both the server's UPS *and* the desktop's UPS (USB
attached to this server) are declared here. The orchestrator distinguishes
between them via the `role` field in `ups_config.yml` (see below).

```ini
[server-ups]
    driver = usbhid-ups
    port = auto
    serial = "<server-ups-serial>"   # pin by serial to survive USB reordering
    desc = "Server UPS"

[desktop-ups]
    driver = usbhid-ups
    port = auto
    serial = "<desktop-ups-serial>"
    desc = "Desktop UPS (USB-attached to server)"
```

> Use `lsusb` and `upsc` to confirm each UPS is reachable. Pinning by
> `serial` (or `vendorid`/`productid`) avoids cases where two UPSes swap
> their `/dev/usb/hiddev*` numbers across reboots.

#### `upsd.conf`
Configures the `upsd` daemon to listen for local connections.
```ini
LISTEN 127.0.0.1 3493
```

#### `upsd.users`
Defines users that can monitor or manage the UPSes. Two users are needed:
the standard `upsmon` user (read-only, used by `upsmon` itself) and a
separate `upscmd_admin` user that the orchestrator uses to issue instant
commands (e.g. `shutdown.return` on the desktop's UPS).

```ini
[upsmon]
    password = mypass
    upsmon master

[upscmd_admin]
    password = anotherpass
    actions  = SET
    instcmds = ALL
```

> Match these credentials with the corresponding env vars:
> `UPS_UPSCMD_USER` / `UPS_UPSCMD_PASS` in `/etc/ups-orchestrator/server.env`.

### 3. Monitoring & Scheduler Configuration

#### `upsmon.conf`
Monitors both UPSes and defines the command to run on events. The crucial
difference between the two is the **powerval** column (4th field):

* `1` — primary UPS (server's own). Counts toward the shutdown threshold:
  if it reaches LOWBATT, the server shuts itself down.
* `0` — observer UPS (desktop's). State changes are still reported, but
  this UPS never causes the server to shut down. The orchestrator handles
  it via `upscmd <ups> shutdown.return` instead.

```ini
MONITOR server-ups@localhost  1 upsmon mypass master
MONITOR desktop-ups@localhost 0 upsmon mypass master

NOTIFYCMD /sbin/upssched
NOTIFYFLAG ONBATT  EXEC+SYSLOG
NOTIFYFLAG ONLINE  EXEC+SYSLOG
NOTIFYFLAG LOWBATT EXEC+SYSLOG

# Ensure all UPSes power off after the server shuts down.
# `upsdrvctl shutdown` (invoked by NUT during system shutdown) will issue
# the configured shutdown command to every UPS in ups.conf — so both the
# server-ups and desktop-ups will be powered off together.
POWERDOWNFLAG /etc/killpower
```

#### `upssched.conf`
The scheduler that executes our scripts. The server package ships a template;
copy it and adjust the UPS ids to match `ups_config.yml`:
```bash
sudo cp /usr/share/ups-orchestrator/server/upssched.conf /etc/nut/upssched.conf
sudo $EDITOR /etc/nut/upssched.conf
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

## Desktop side

The desktop does **not** run NUT in this architecture. Both UPSes (the
server's and the desktop's) are USB-attached to the server, so the server
is the single point of UPS coordination. This solves the "second outage
while desktop is already off" problem — the server can still cleanly power
off the desktop's UPS to protect its battery.

The desktop only runs the orchestrator agent. When the server pushes
`critical_shutdown`, the agent shuts the host down via its built-in
fallback chain:

1. `sudo upsmon -c fsd` (preserved for legacy installs that still run NUT
   on the desktop — fails silently if NUT isn't installed)
2. `systemctl poweroff`
3. `sudo /sbin/shutdown -h now`

Only the third one needs a sudoers rule on the desktop:

```bash
sudo visudo -f /etc/sudoers.d/ups-orchestrator-agent
```

```
# Adjust the user to match the desktop agent's account
your-user ALL=(root) NOPASSWD: /sbin/shutdown
```

> If you keep NUT on the desktop (legacy / mixed setup), also add the
> `upsmon -c fsd` rules — see git history of this file for the previous
> sudoers block.

---

## Integration with UPS Orchestrator

The server uses `upssched-cmd` (installed as `/usr/libexec/ups-orchestrator/upssched-cmd`)
to process events from NUT; `CMDSCRIPT` in the template already points there. It runs as
NUT's user and reads the port and token from `/etc/ups-orchestrator/server.env`, which the
package makes readable to NUT's user by adding it to the `ups-orchestrator` group. Restart
NUT once after installing (`sudo systemctl restart nut-monitor`) so the running `upsmon`
picks up the new group.

---

## Installation (RPM)

Both components are published as RPMs to the Gitea package registry by CI (see
[packaging/ci/README.md](packaging/ci/README.md)):

| Package | Installed on | Repository group |
|---|---|---|
| `ups-orchestrator-server` | the NUT host (Rocky Linux 10, needs EPEL) | `el/10` |
| `ups-orchestrator-desktop` | the desktop (Fedora 44) | `fedora/44` |

Add the repository once (replace `<instance>`, `<owner>` and the group):

```ini
# /etc/yum.repos.d/ups-orchestrator.repo
[ups-orchestrator]
name=UPS Orchestrator
baseurl=https://<instance>/api/packages/<owner>/rpm/el/10
enabled=1
gpgcheck=1
gpgkey=https://<instance>/api/packages/<owner>/rpm/repository.key
```

Then `sudo dnf install ups-orchestrator-server` (or `-desktop`); later releases arrive with
`sudo dnf upgrade`.

Layout:

| What | Where |
|---|---|
| Code | `/usr/share/ups-orchestrator/{server,desktop}/app/` |
| Configuration | `/etc/ups-orchestrator/server.env`, `ups_config.yml`, `desktop.env` (kept on upgrade) |
| Server state / logs | `/var/lib/ups-orchestrator`, `/var/log/ups-orchestrator` |
| NUT hook | `/usr/libexec/ups-orchestrator/upssched-cmd` |

### Running from a checkout (development)

```bash
python -m venv server/.venv && server/.venv/bin/pip install -r server/requirements.txt
cp server/.env.example server/.env
(set -a; . server/.env; set +a; server/.venv/bin/python server/app/server.py)

# The desktop needs python3-gobject from the system for gi/pydbus
python -m venv --system-site-packages desktop/.venv
desktop/.venv/bin/pip install -r desktop/requirements.txt
cp desktop/.env.example desktop/.env
desktop/.venv/bin/python desktop/app/agent.py
```

---

## Usage & Simulation

Substitute `<ups_id>` below with the id from `ups_config.yml`
(e.g. `server-ups`, `desktop-ups`).

### Test Power Outage (ONBATT)
```bash
curl -X POST \
  -H "X-UPS-Token: secret-token" \
  -H "Content-Type: application/json" \
  -d '{"event": "ONBATT"}' \
  http://localhost:8787/api/ups/<ups_id>/event
```

### Test Power Restoration (ONLINE)
```bash
curl -X POST \
  -H "X-UPS-Token: secret-token" \
  -H "Content-Type: application/json" \
  -d '{"event": "ONLINE"}' \
  http://localhost:8787/api/ups/<ups_id>/event
```

### List configured UPSes
```bash
curl -H "X-UPS-Token: secret-token" http://localhost:8787/api/ups/
```

---

## Deployment (systemd)

The packages install the units; they start nothing until enabled.

### 1. Server
1. Edit `/etc/ups-orchestrator/server.env` (token, upscmd credentials) and
   `/etc/ups-orchestrator/ups_config.yml` (UPS ids, desktop agent URL, MAC address).
2. Run:
   ```bash
   sudo systemctl enable --now ups-orchestrator-server
   ```

The service runs as the `ups-orchestrator` system user with `CAP_NET_RAW` for Wake-on-LAN.
The package installs `/etc/sudoers.d/ups-orchestrator-server`, which lets that user run
`upsmon -c fsd` for the self-shutdown. A `wol_relay` uses that user's SSH key, under
`/var/lib/ups-orchestrator/.ssh/`.

### 2. Desktop
1. Edit `/etc/ups-orchestrator/desktop.env` (token, server URL).
2. Run:
   ```bash
   # Power agent: reports suspend/shutdown to the server (system service)
   sudo systemctl enable --now ups-power-agent
   # Agent: receives commands and prompts the user (user service, per login)
   systemctl --user enable --now ups-orchestrator-agent
   ```
   *Note: Running as a user service ensures the agent has access to your DBUS session for notifications and UI dialogs.*

### Moving from an `/opt/ups-orchestrator` install
Stop and disable the old units, delete the copies under `/etc/systemd/system/` and
`~/.config/systemd/user/`, move the old `.env` / `ups_config.yml` contents into
`/etc/ups-orchestrator/`, point `/etc/nut/upssched.conf` at the packaged template, and remove
the old `/etc/sudoers.d/ups-orchestrator-server`. The old state under
`/opt/ups-orchestrator/server/.runtime-state` and `logs/` can be copied to
`/var/lib/ups-orchestrator` and `/var/log/ups-orchestrator` to keep the outage history.

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

### 2. Troubleshooting
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
The system `python3` usually has no pytest, so run the tests from a venv:
```bash
python3 -m venv .venv-test
.venv-test/bin/python -m pip install -r requirements-test.txt
.venv-test/bin/python -m pytest -q -n auto tests/
```
`-n auto` spreads the tests over one worker process per CPU (pytest-xdist); drop it to run them
serially.

CI (`.gitea/workflows/ci.yml`) does not run on branch pushes, so run the tests locally before
tagging. A `v*` tag matching `Version:` in `packaging/rpm/ups-orchestrator.spec` runs the tests,
then builds and publishes both RPMs. `tools/set-version.sh`
sets that version and prints the commit and tag commands (see
[packaging/ci/README.md](packaging/ci/README.md#release)).

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
© 2026 Onur T.