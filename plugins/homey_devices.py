#!/usr/bin/env python3
"""homey_devices.py - Capability values of Homey devices. No external deps.

Device data lives inside the Homey app and cannot be read from a plugin
subprocess, so this plugin talks to the collector bridge of the pymon Homey app
(PYMON_HOMEY_BRIDGE). Configure one entry per device with the capabilities you
want to monitor.

Emits one metric per device and capability, named "<device_id>.<capability>".
Unknown device ids are reported as ``devices_unknown`` so a typo in the config
is visible instead of silently producing nothing.

If the bridge is not configured the plugin exits non-zero: on a plain Linux
agent this plugin simply does not apply.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

__schema__ = {
    "label": "Homey Devices",
    "description": "Capability values of Homey devices (temperature, power, battery, ...)",
    "fields": [
        {"key": "sleep", "label": "Interval (s)", "type": "number", "default": 60, "min": 5},
        {
            "key": "devices",
            "label": "Devices",
            "type": "array:object",
            "default": [],
            "fields": [
                {"key": "device_id", "label": "Device ID", "type": "string"},
                {
                    "key": "capabilities",
                    "label": "Capabilities",
                    "type": "array:string",
                    "default": [],
                },
            ],
        },
    ],
}

BRIDGE_ENV = "PYMON_HOMEY_BRIDGE"
TOKEN_ENV = "PYMON_HOMEY_BRIDGE_TOKEN"


def _bridge_get(path, params=None, timeout=8):
    """GET a collector bridge endpoint. Returns None when unavailable."""
    base = os.environ.get(BRIDGE_ENV)
    if not base:
        return None
    url = f"{base}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    request = urllib.request.Request(
        url,
        headers={"X-Pymon-Bridge-Token": os.environ.get(TOKEN_ENV, "")},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        # A refused token and an unreachable bridge are indistinguishable here,
        # and both mean the same thing to the caller: no data available.
        return None


def _selection(config):
    """Build the device -> capabilities mapping, preserving per-device scope."""
    devices = []
    for entry in config.get("devices") or []:
        if not isinstance(entry, dict):
            continue
        device_id = str(entry.get("device_id") or "").strip()
        if not device_id:
            continue
        capabilities = []
        for capability in entry.get("capabilities") or []:
            capability = str(capability).strip()
            if capability and capability not in capabilities:
                capabilities.append(capability)
        devices.append((device_id, capabilities))
    return devices


if __name__ == "__main__":
    config = json.load(sys.stdin)

    if not os.environ.get(BRIDGE_ENV):
        sys.stderr.write(
            "homey_devices requires the pymon Homey app collector bridge; "
            "no bridge address in the environment\n"
        )
        sys.exit(1)

    devices = _selection(config)
    if not devices:
        sys.stderr.write("no devices configured in pymon\n")
        sys.exit(1)

    # Send the device -> capabilities mapping instead of one flat capability
    # list: a device must only report what it was configured for, otherwise a
    # capability configured on one device leaks onto all the others.
    payload = _bridge_get(
        "/devices",
        {"map": json.dumps(dict(devices))},
    )
    if payload is None:
        sys.stderr.write("collector bridge did not answer /devices\n")
        sys.exit(1)

    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        sys.stderr.write("collector bridge returned no values\n")
        sys.exit(1)

    metrics = dict(values)
    metrics["devices_reported"] = len(values)
    metrics["devices_unknown"] = len(payload.get("unknown_devices") or [])
    print(json.dumps(metrics))