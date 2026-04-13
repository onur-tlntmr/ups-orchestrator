"""Wake-on-LAN utilities.

Supports two sending strategies:
- Local: tries AF_PACKET (raw L2), UDP directed broadcast, UDP global broadcast.
- SSH relay: runs a Python one-liner on a remote host via passwordless SSH.
  Useful when the server is a Proxmox VM and the bridge does not forward
  broadcast frames to the physical NIC.
"""

import logging
import socket
import subprocess

logger = logging.getLogger(__name__)

WOL_UDP_PORT = 9


def send(
    mac_address: str,
    *,
    iface: str = "",
    relay_ssh: str = "",
    relay_identity_file: str = "",
) -> None:
    """Send a WoL magic packet to mac_address.

    Args:
        mac_address:          Target MAC, any common separator accepted ("AA:BB:CC:DD:EE:FF").
        iface:                Local interface hint for AF_PACKET / directed-broadcast UDP.
        relay_ssh:            If set (e.g. "root@192.168.50.1"), delegate to that host via SSH.
        relay_identity_file:  SSH private key to use with relay_ssh (optional).

    Raises:
        ValueError:   Invalid MAC address.
        RuntimeError: All local methods failed (only when relay_ssh is not set).
    """
    mac_bytes = _parse_mac(mac_address)
    magic = b"\xff" * 6 + mac_bytes * 16

    if relay_ssh:
        _send_via_ssh(relay_ssh, mac_address, magic, identity_file=relay_identity_file)
    else:
        _send_local(magic, iface)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_mac(mac_address: str) -> bytes:
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    if len(mac_bytes) != 6:
        raise ValueError(f"Invalid MAC address: {mac_address!r}")
    return mac_bytes


def _send_local(magic: bytes, iface: str) -> None:
    errors = []

    # Method 1: raw Layer 2 Ethernet frame — ether-wake equivalent (needs CAP_NET_RAW)
    try:
        frame = b"\xff" * 6 + b"\x00" * 6 + b"\x08\x42" + magic
        with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0842)) as s:
            s.bind((iface, 0))
            s.send(frame)
        logger.debug(f"WoL sent via AF_PACKET on {iface!r}")
        return
    except Exception as exc:
        errors.append(f"AF_PACKET: {exc}")

    # Method 2: UDP to directed broadcast of the interface
    try:
        bcast = _broadcast_for_iface(iface)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(magic, (bcast, WOL_UDP_PORT))
        logger.debug(f"WoL sent via UDP to {bcast}:{WOL_UDP_PORT}")
        return
    except Exception as exc:
        errors.append(f"UDP directed ({bcast}): {exc}")

    # Method 3: UDP to global broadcast
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(magic, ("255.255.255.255", WOL_UDP_PORT))
        logger.debug(f"WoL sent via UDP to 255.255.255.255:{WOL_UDP_PORT}")
        return
    except Exception as exc:
        errors.append(f"UDP global: {exc}")

    raise RuntimeError(f"All WoL methods failed: {'; '.join(errors)}")


def _send_via_ssh(
    relay_ssh: str,
    mac_address: str,
    magic: bytes,
    identity_file: str = "",
) -> None:
    """Delegate magic-packet transmission to a remote host over SSH.

    Runs a self-contained Python one-liner — no additional packages needed on
    the remote host.  Requires passwordless (key-based) SSH access.
    """
    mac_hex = mac_address.replace(":", "").replace("-", "")
    remote_cmd = (
        f"python3 -c \""
        f"import socket; "
        f"mac=bytes.fromhex('{mac_hex}'); "
        f"magic=b'\\xff'*6+mac*16; "
        f"s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
        f"s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1); "
        f"s.sendto(magic,('255.255.255.255',9)); "
        f"s.close()\""
    )
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if identity_file:
        cmd += ["-i", identity_file]
    cmd += [relay_ssh, remote_cmd]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH WoL relay failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    logger.debug(f"WoL sent via SSH relay {relay_ssh!r}")


def iface_for_ip(target_ip: str) -> str:
    """Return the network interface that routes to target_ip."""
    try:
        result = subprocess.run(
            ["ip", "route", "get", target_ip],
            capture_output=True, text=True, timeout=5,
        )
        parts = result.stdout.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return "eth0"


def _broadcast_for_iface(iface: str) -> str:
    """Return the broadcast address of the given interface."""
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True, text=True, timeout=5,
        )
        parts = result.stdout.split()
        if "brd" in parts:
            return parts[parts.index("brd") + 1]
    except Exception:
        pass
    return "255.255.255.255"
