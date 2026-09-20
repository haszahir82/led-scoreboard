"""Board name, hostname, and mDNS.

Each board gets a name during setup ("Mike's Board"). That becomes:

  * the hostname            mikes-board
  * the mDNS address        mikes-board.local
  * the title in the web UI

The privilege drop makes this awkward. hzeller's matrix library setuids to
`daemon` after claiming GPIO, so by the time the web server is handling a
rename it can no longer call hostnamectl. Rather than granting the web process
root or a sudoers entry, a rename writes a request file that a tiny root-owned
systemd path unit picks up and applies. The web process only ever writes a
file in a directory it already owns.

main.py also applies any pending rename at startup, while it is still root,
which covers the reboot case and means the path unit is an optimization rather
than a requirement.
"""

import os
import re
import socket
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
REQUEST_PATH = os.path.join(STATE_DIR, "hostname.request")

RESERVED = {"localhost", "raspberrypi", "local", "gateway", "router"}
MAX_LEN = 32


def slugify(name, fallback="scoreboard"):
    """'Mike's Board!' -> 'mikes-board'.

    RFC 1123 rules: lowercase alphanumerics and hyphens, no leading or
    trailing hyphen. Apostrophes are dropped rather than becoming hyphens so
    "Mike's" reads as "mikes" and not "mike-s".
    """
    text = str(name or "").strip().lower()
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")[:MAX_LEN].strip("-")
    if not text or text in RESERVED or text[0].isdigit():
        suffix = text or ""
        text = "{}-{}".format(fallback, suffix).strip("-")[:MAX_LEN].strip("-")
    return text or fallback


def current_hostname():
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return ""


def mdns_name(hostname=None):
    return "{}.local".format(hostname or current_hostname())


def request_rename(hostname):
    """Queue a hostname change for the root helper. Safe to call as `daemon`."""
    hostname = slugify(hostname)
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = REQUEST_PATH + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(hostname + "\n")
    os.replace(tmp, REQUEST_PATH)
    return hostname


def pending_rename():
    try:
        with open(REQUEST_PATH) as fh:
            return slugify(fh.read().strip())
    except Exception:
        return None


def clear_request():
    try:
        os.remove(REQUEST_PATH)
    except Exception:
        pass


def apply_hostname(hostname):
    """Actually set the hostname. Must run as root.

    Updates /etc/hosts alongside hostnamectl, because leaving the old name in
    /etc/hosts makes sudo hang for ten seconds on every invocation while it
    fails to resolve the machine's own name.
    """
    hostname = slugify(hostname)
    if not hostname or hostname == current_hostname():
        return False

    subprocess.run(["hostnamectl", "set-hostname", hostname], check=True,
                   timeout=15)

    try:
        with open("/etc/hosts") as fh:
            lines = fh.readlines()
        out, replaced = [], False
        for line in lines:
            if line.strip().startswith("127.0.1.1"):
                out.append("127.0.1.1\t{}\n".format(hostname))
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append("127.0.1.1\t{}\n".format(hostname))
        with open("/etc/hosts", "w") as fh:
            fh.writelines(out)
    except Exception:
        pass

    # Avahi publishes <hostname>.local; it must be told the name changed.
    subprocess.run(["systemctl", "restart", "avahi-daemon"], check=False,
                   timeout=20)
    return True


def apply_pending():
    """Apply a queued rename. Called from main.py at startup, while root."""
    wanted = pending_rename()
    if not wanted:
        return None
    try:
        changed = apply_hostname(wanted)
    except Exception:
        return None
    clear_request()
    return wanted if changed else None


if __name__ == "__main__":
    # Entry point for the root-owned systemd helper unit.
    applied = apply_pending()
    print("hostname set to {}".format(applied) if applied else "no change")
