"""Credential storage, kept out of config.json and out of the web API.

config.json is served whole at /api/config, unauthenticated, to anything on the
local network. That is fine for teams and brightness. It is not fine for an
ESPN session cookie, which is account access rather than a scoped key: anyone
on the wifi could read it from a browser.

So secrets live in their own file that the API never returns. The API reports
only whether a credential is present and whether it last worked, which is all
the settings page needs to render its state.

This is not encryption. Anyone with the SD card or a shell can read the file,
and there is nowhere on a Pi to hide a key from someone holding the hardware.
What it does buy is the difference between "you need physical access" and
"anyone on the guest wifi", which is the difference that matters.
"""

import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_PATH = os.path.join(ROOT, "state", "secrets.json")

_lock = threading.Lock()


def _load():
    try:
        with open(SECRETS_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    directory = os.path.dirname(SECRETS_PATH)
    os.makedirs(directory, exist_ok=True)
    tmp = SECRETS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    # Owner and group only. The render process runs as `daemon` after the
    # privilege drop and has to be able to read this back.
    try:
        os.chmod(tmp, 0o660)
    except Exception:
        pass
    os.replace(tmp, SECRETS_PATH)


def get(namespace, key=None, default=None):
    with _lock:
        data = _load().get(namespace) or {}
    if key is None:
        return data
    return data.get(key, default)


def put(namespace, values):
    """Merge values into a namespace. A None value deletes that key."""
    with _lock:
        data = _load()
        bucket = data.get(namespace) or {}
        for key, value in (values or {}).items():
            if value is None:
                bucket.pop(key, None)
            else:
                bucket[key] = value
        if bucket:
            data[namespace] = bucket
        else:
            data.pop(namespace, None)
        _save(data)
    return True


def clear(namespace):
    with _lock:
        data = _load()
        existed = namespace in data
        data.pop(namespace, None)
        _save(data)
    return existed


def has(namespace, *required):
    values = get(namespace) or {}
    return all(values.get(name) for name in required)


def redact(value, keep=4):
    """'AEBxyz...123' -> '...z123', for showing that something is stored."""
    text = str(value or "")
    if len(text) <= keep:
        return "*" * len(text)
    return "..." + text[-keep:]
