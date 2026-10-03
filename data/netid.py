"""Where the board is on the network, and whether that is somewhere new.

A board that has just joined a network has one job before anything else: tell
whoever plugged it in how to reach its settings page. The mDNS name is the
nicer answer and the unreliable one -- plenty of phones and routers do not
resolve .local, which is exactly the moment a brother gives up. The IP always
works, and the board is the only thing that knows it.

"Somewhere new" is deliberately a question about the network rather than about
the address. A router handing out a different lease after a power cut has not
moved the board anywhere, and announcing again every time DHCP shuffles would
turn a helpful screen into a recurring annoyance in somebody's living room.
So the fingerprint is the wifi network when there is one, and the subnet when
there is not: taking the board to another house changes it, a new lease on the
same network does not.
"""

import json
import os
import re
import socket
import subprocess

STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
SEEN_PATH = os.path.join(STATE_DIR, "announced-networks.json")


def ip_address():
    """This board's address on the LAN, or "" when it has none.

    Opening a UDP socket to a public address and asking what local address the
    kernel chose is the reliable way to get this. It sends nothing -- UDP
    connect only sets the peer -- and unlike parsing `hostname -I` it picks the
    interface that actually carries traffic rather than the first one listed,
    which on a Pi with both wifi and a stale bridge is not the same thing.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.connect(("192.0.2.1", 9))      # TEST-NET-1: routable, never answers
        return sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def wifi_ssid():
    """The joined wifi network's name, or "" on Ethernet or no radio."""
    for cmd in (["iwgetid", "-r"],
                ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=5).stdout.strip()
        except Exception:
            continue
        if not out:
            continue
        if cmd[0] == "iwgetid":
            return out.splitlines()[0].strip()
        for line in out.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1].strip()
    return ""


def subnet(ip=None):
    """The /24 an address sits in, as a coarse stand-in for "which network"."""
    ip = ip if ip is not None else ip_address()
    match = re.match(r"^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$", ip or "")
    return match.group(1) if match else ""


def fingerprint(ip=None, ssid=None):
    """A stable name for the network this board is on.

    Prefers the SSID, because it survives a DHCP reshuffle and distinguishes
    two houses that both use 192.168.1.x -- which most houses do, and which a
    subnet alone would call the same place.
    """
    ssid = wifi_ssid() if ssid is None else ssid
    if ssid:
        return "wifi:" + ssid
    net = subnet(ip)
    return "lan:" + net if net else ""


def _load():
    try:
        with open(SEEN_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def has_history():
    """Has this board ever recorded a network at all?

    Used to tell a genuinely new network apart from the first run after an
    upgrade. Without it, every board already sitting happily on its owner's
    wifi would decide that wifi was unfamiliar and put an IP address on the
    panel for five minutes -- a working board behaving oddly because of a
    feature meant for boards that have moved.
    """
    return os.path.exists(SEEN_PATH)


def already_announced(print_=None):
    """Has the board already shown its address on this network?"""
    key = fingerprint() if print_ is None else print_
    return bool(key) and key in _load()


def remember(print_=None, address=""):
    """Record that this network has been told. Best effort; never raises."""
    key = fingerprint() if print_ is None else print_
    if not key:
        return
    data = _load()
    data[key] = address
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SEEN_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, SEEN_PATH)
    except Exception:
        pass


def forget_all():
    """Clear the record, so the next network counts as new again.

    Called by the handoff reset: a board being given away has to announce
    itself in its new home even though it already announced itself here.
    """
    try:
        os.remove(SEEN_PATH)
    except Exception:
        pass
