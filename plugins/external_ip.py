#!/usr/bin/env python3
"""external_ip.py — public IP detection with change tracking. Stdlib only.

Queries several keyless provider APIs until one returns a valid public IP.
The current address is emitted as an informational string; any glue needed
for alerting is exposed as numbers:

  external_ip_ok            1 = an address was resolved, 0 = all providers failed
  external_ip_changed       1 = address differs from last known, 0 = unchanged
  external_ip_change_count  cumulative number of IP changes (persisted)
  external_ip_change_ts     unix timestamp of the last change (seconds)
  external_ip_latency_ms    latency of the serving provider request
  external_ip_provider      provider name that served the address (string)
  external_ip_value         the resolved address (string)

A total provider failure never marks the address as changed, so connection
problems cannot trigger a false IP-change alarm.
"""
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request

__schema__ = {
    'label': 'External IP',
    'description': 'Public IP address and change detection (multi-provider, keyless)',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 300, 'min': 30},
        {'key': 'timeout', 'label': 'Timeout per provider (s)', 'type': 'number', 'default': 6, 'min': 2, 'max': 10},
        {'key': 'state_file', 'label': 'State file (optional)', 'type': 'string', 'default': '', 'optional': True},
    ],
}

PROVIDERS = [
    ("ipify", "https://api.ipify.org"),
    ("ifconfig.me", "https://ifconfig.me/ip"),
    ("icanhazip", "https://icanhazip.com"),
]

STATE_VERSION = 1


def _query_provider(url, timeout):
    """Return (ip, latency_ms) or (None, None) on any failure."""
    start = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None, None
    latency_ms = round((time.time() - start) * 1000.0, 1)
    try:
        ipaddress.ip_address(body)
    except ValueError:
        return None, None
    return body, latency_ms


def _resolve(config):
    """Return (provider, ip, latency_ms) of the first provider that succeeds."""
    try:
        timeout = max(2, min(10, int(config.get("timeout", 6))))
    except (TypeError, ValueError):
        timeout = 6
    for name, url in PROVIDERS:
        ip, latency_ms = _query_provider(url, timeout)
        if ip:
            return name, ip, latency_ms
    return None, None, None


def _default_state_file():
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(plugin_dir, ".external_ip_state.json")


def _load_state(path):
    fresh = {"_version": STATE_VERSION, "last_ip": None, "change_count": 0, "last_change_ts": None}
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return fresh
        for k, v in fresh.items():
            state.setdefault(k, v)
        state["_version"] = STATE_VERSION
        return state
    except (OSError, ValueError):
        return fresh


def _save_state(path, state):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def run(config):
    state_file = config.get("state_file") or _default_state_file()
    state = _load_state(state_file)

    provider, ip, latency_ms = _resolve(config)
    if not ip:
        print(json.dumps({
            "external_ip_ok": 0,
            "external_ip_change_count": state.get("change_count", 0),
        }))
        return

    changed = 0
    if state.get("last_ip") != ip:
        state["last_ip"] = ip
        state["change_count"] = state.get("change_count", 0) + 1
        state["last_change_ts"] = time.time()
        changed = 1

    _save_state(state_file, state)

    metrics = {
        "external_ip_ok": 1,
        "external_ip_changed": changed,
        "external_ip_change_count": state["change_count"],
        "external_ip_provider": provider,
        "external_ip_value": ip,
    }
    if latency_ms is not None:
        metrics["external_ip_latency_ms"] = latency_ms
    if state.get("last_change_ts") is not None:
        metrics["external_ip_change_ts"] = int(state["last_change_ts"])
    print(json.dumps(metrics))


if __name__ == "__main__":
    try:
        cfg = json.load(sys.stdin)
    except ValueError:
        cfg = {}
    try:
        run(cfg)
    except Exception:
        print(json.dumps({"external_ip_ok": 0, "external_ip_error": "internal"}))