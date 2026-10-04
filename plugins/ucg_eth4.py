#!/usr/bin/env python3
"""ucg_eth4.py — Local read-only probe of the UCG eth4 uplink and ppp0 PPPoE state.

The plugin is a *local* OS/network plugin: it must be executed by a pymon agent
running **on the UCG itself**, because eth4 and ppp0 only exist in the UCG's
own kernel. It reads /sys/class/net/<iface>/... directly and asks the local
`ip` binary for the PPPoE address. There is deliberately no remote transport:
no SSH, no serial console, no vendor API, no other host.

Everything the plugin does is read-only:
  * sysfs files below /sys/class/net/<iface> are opened for reading,
  * the only subprocess is `ip -4 -o addr show dev <iface> scope global`
    (argument list, shell=False, short timeout),
  * the state file below /tmp holds counters and bookkeeping only.

Counter semantics: cumulative kernel counters are exposed as *_total plus a
delta against the last *successful* probe. A delta > 0 means "at least one
link/error event happened since the last probe" — never "the cable is
broken". Counter resets (reboot, driver reload) are flagged separately with
ucg_probe_counter_reset=1 and a delta of 0 instead of a bogus large value.
The first successful probe only establishes a baseline, all deltas are 0.
"""
import ipaddress
import json
import os
import re
import subprocess
import sys
import time

__schema__ = {
    "label": "UCG eth4 (local)",
    "description": "Local read-only probe of the UCG eth4 uplink (sysfs) and ppp0 PPPoE state",
    "fields": [
        {"key": "sleep", "label": "Interval (s)", "type": "number", "default": 30, "min": 30},
        {"key": "interface", "label": "Ethernet interface", "type": "string", "default": "eth4"},
        {"key": "pppoe_interface", "label": "PPPoE interface", "type": "string", "default": "ppp0"},
        {"key": "state_file", "label": "State file (optional)", "type": "string", "default": "", "optional": True},
    ],
}

SYSFS_NET = os.environ.get("PYMON_UCG_SYSFS_ROOT") or "/sys/class/net"
DEFAULT_STATE_FILE = "/tmp/pymon_ucg_eth4_state.json"
PPPOE_QUERY_TIMEOUT = 5
STATE_VERSION = 1

# Linux interface names are at most IFNAMSIZ-1 characters and may not contain
# '/' or whitespace. The pattern is the only gate between config values and
# file paths / argument lists, so it is deliberately strict.
IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}$")

OPERSTATES = {"up", "down", "unknown", "dormant", "notpresent", "lowerlayerdown"}

# Primary values: all must be present and parseable or the whole ethernet
# probe is reported as failed (no stale values are emitted).
PRIMARY_COUNTERS = ("carrier", "carrier_changes", "rx_errors", "rx_crc_errors")

# Closed vocabularies: metric values never carry free-form error text.
ETH_ERRORS = ("none", "missing_interface", "missing_counter", "read_error", "parse_error", "invalid_config")
PPPOE_ERRORS = ("none", "interface_down", "command_missing", "command_timeout", "command_failed", "parse_error")


# ---------------------------------------------------------------------------
# config / path handling
# ---------------------------------------------------------------------------

def _clean_iface(value):
    """Return a validated interface name, or None if it is unusable."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not IFACE_RE.match(name) or ".." in name:
        return None
    return name


def _iface_dir(sysfs_root, iface):
    """Join the interface directory and refuse to escape the sysfs root."""
    root = os.path.abspath(sysfs_root)
    path = os.path.abspath(os.path.join(root, iface))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path


def _state_path(config):
    configured = config.get("state_file")
    if isinstance(configured, str) and configured.strip():
        candidate = configured.strip()
        if "\x00" in candidate:
            return DEFAULT_STATE_FILE
        return candidate
    return DEFAULT_STATE_FILE


# ---------------------------------------------------------------------------
# sysfs readers
# ---------------------------------------------------------------------------

def _read_text(path):
    """Return stripped file content, or raise OSError."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def _read_int(base, relpath):
    """Read a sysfs counter. Returns (value, error) with error in
    (None, 'missing_counter', 'read_error', 'parse_error')."""
    path = os.path.join(base, relpath)
    if not os.path.exists(path):
        return None, "missing_counter"
    try:
        raw = _read_text(path)
    except OSError:
        return None, "read_error"
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "parse_error"


def collect_ethernet(iface, sysfs_root=SYSFS_NET):
    """Read the primary and optional sysfs values for one interface.

    Returns (values, error) where error is None on success or one of the
    ETH_ERRORS classes. No exception escapes: a failed probe must never take
    the agent down.
    """
    base = _iface_dir(sysfs_root, iface)
    if base is None or not os.path.isdir(base):
        return {}, "missing_interface"

    values = {}
    for name in PRIMARY_COUNTERS:
        rel = name if name in ("carrier", "carrier_changes") else os.path.join("statistics", name)
        value, err = _read_int(base, rel)
        if err is not None:
            return {}, err
        if value < 0 or (name == "carrier" and value not in (0, 1)):
            return {}, "parse_error"
        values[name] = value

    # Optional extras are best-effort: absent, unreadable or nonsensical values
    # are omitted rather than reported as 0.
    try:
        operstate = _read_text(os.path.join(base, "operstate"))
        if operstate.lower() in OPERSTATES:
            values["operstate"] = operstate.lower()
    except OSError:
        pass

    speed, err = _read_int(base, "speed")
    if err is None and speed > 0:
        values["speed"] = speed

    try:
        duplex = _read_text(os.path.join(base, "duplex")).lower()
        if duplex in ("full", "half"):
            values["duplex"] = duplex
    except OSError:
        pass

    return values, None


# ---------------------------------------------------------------------------
# PPPoE probe (local, read-only)
# ---------------------------------------------------------------------------

def _run_ip(argv, timeout):
    """subprocess wrapper, injected in tests. Never uses shell=True."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False, check=False)


def _parse_global_ipv4(output):
    """Extract the first global IPv4 address from `ip -o addr show` output.

    Returns (address, ok). ok is False only when the output is not empty but
    cannot be parsed — an empty output is a valid "no global address". The
    scope is re-checked here instead of trusting the query filter, so a
    link-local or host address can never be published as the PPPoE address.
    """
    if not output or not output.strip():
        return None, True
    for line in output.splitlines():
        parts = line.split()
        if "inet" not in parts or "scope" not in parts:
            continue
        scope = parts.index("scope")
        if scope + 1 >= len(parts) or parts[scope + 1] != "global":
            continue
        idx = parts.index("inet")
        if idx + 1 >= len(parts):
            continue
        candidate = parts[idx + 1].split("/")[0]
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if addr.version == 4 and not addr.is_loopback and not addr.is_unspecified:
            return str(addr), True
    return None, False


def collect_pppoe(iface, sysfs_root=SYSFS_NET, runner=None, timeout=PPPOE_QUERY_TIMEOUT):
    """Read the local PPPoE state of ``iface``.

    Returns (up, address, query_success, query_error). A missing interface or
    a missing global IPv4 address is a *valid* measurement (up=0), not a
    failed query. Only a missing binary, a timeout, a non-zero exit or
    unparseable output mark the query itself as unsuccessful.
    """
    runner = runner or _run_ip
    up = 0
    address = ""

    base = _iface_dir(sysfs_root, iface)
    if base is None or not os.path.isdir(base):
        return up, address, 1, "interface_down"

    argv = ["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"]
    try:
        proc = runner(argv, timeout)
    except FileNotFoundError:
        return up, address, 0, "command_missing"
    except subprocess.TimeoutExpired:
        return up, address, 0, "command_timeout"
    except OSError:
        return up, address, 0, "command_failed"

    if proc.returncode != 0:
        return up, address, 0, "command_failed"

    parsed, ok = _parse_global_ipv4(proc.stdout or "")
    if not ok:
        return up, address, 0, "parse_error"
    if parsed:
        return 1, parsed, 1, "none"
    return up, address, 1, "none"


# ---------------------------------------------------------------------------
# state handling
# ---------------------------------------------------------------------------

def _fresh_state(iface, pppoe_iface):
    return {
        "_version": STATE_VERSION,
        "interface": iface,
        "pppoe_interface": pppoe_iface,
        "baseline": False,
        "last_success_at": None,
        "counters": {},
    }


def _load_state(path, iface, pppoe_iface):
    """Load the counter state, discarding it if it belongs to another device."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
    except (OSError, ValueError):
        return _fresh_state(iface, pppoe_iface)

    if state.get("interface") != iface or state.get("pppoe_interface") != pppoe_iface:
        return _fresh_state(iface, pppoe_iface)
    counters = state.get("counters")
    if not isinstance(counters, dict):
        counters = {}
    clean = {}
    for key, value in counters.items():
        if key in ("carrier_changes", "rx_errors", "rx_crc_errors") and isinstance(value, int) and not isinstance(value, bool):
            clean[key] = value
    state["_version"] = STATE_VERSION
    state["counters"] = clean
    state["baseline"] = bool(state.get("baseline"))
    return state


def _save_state(path, state):
    """Atomically persist the technical counter state."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _delta(state, values, now, metrics):
    """Emit totals + deltas for the monotonic counters, detect resets.

    Returns True when at least one counter was reset.
    """
    was_baseline = bool(state.get("baseline"))
    previous = state.get("counters", {})
    any_reset = False

    for key in ("carrier_changes", "rx_errors", "rx_crc_errors"):
        value = values[key]
        metrics[f"ucg_eth4_{key}_total"] = value
        prev = previous.get(key)
        if prev is None or value < prev:
            if prev is not None:
                any_reset = True
            delta = 0
        else:
            delta = value - prev
        metrics[f"ucg_eth4_{key}_delta"] = delta
        state["counters"][key] = value

    metrics["ucg_probe_baseline"] = 0 if was_baseline else 1
    metrics["ucg_probe_counter_reset"] = 1 if any_reset else 0
    state["baseline"] = True
    state["last_success_at"] = now
    return any_reset


# ---------------------------------------------------------------------------
# probe assembly
# ---------------------------------------------------------------------------

def compute_metrics(state, values, pppoe, now=None):
    """Build the metric dict for one poll from a successful ethernet read.

    ``pppoe`` is the (up, address, query_success, query_error) tuple of
    collect_pppoe(); it is reported independently of the ethernet probe.
    """
    if now is None:
        now = time.time()
    metrics = {
        "ucg_probe_success": 1,
        "ucg_probe_error": "none",
        "ucg_eth4_carrier": values["carrier"],
    }
    _delta(state, values, now, metrics)

    if "operstate" in values:
        metrics["ucg_eth4_operstate"] = values["operstate"]
    if "speed" in values:
        metrics["ucg_eth4_speed_mbps"] = values["speed"]
    if "duplex" in values:
        metrics["ucg_eth4_full_duplex"] = 1 if values["duplex"] == "full" else 0

    metrics["ucg_pppoe_up"] = pppoe[0]
    metrics["ucg_pppoe_ip"] = pppoe[1]
    metrics["ucg_pppoe_query_success"] = pppoe[2]
    metrics["ucg_pppoe_query_error"] = pppoe[3]
    return metrics


def _pppoe_metrics(pppoe):
    up, address, query_success, query_error = pppoe
    return {
        "ucg_pppoe_up": up,
        "ucg_pppoe_ip": address,
        "ucg_pppoe_query_success": query_success,
        "ucg_pppoe_query_error": query_error,
    }


def probe(config, sysfs_root=SYSFS_NET, runner=None):
    """Run one complete local probe. Returns (metrics, state, state_path)."""
    iface = _clean_iface(config.get("interface", "eth4"))
    pppoe_iface = _clean_iface(config.get("pppoe_interface", "ppp0"))

    if iface is None or pppoe_iface is None:
        # Unusable interface name: nothing is read, nothing is executed.
        metrics = {
            "ucg_probe_success": 0,
            "ucg_probe_error": "invalid_config",
            "ucg_probe_baseline": 0,
            "ucg_probe_counter_reset": 0,
        }
        metrics.update(_pppoe_metrics((0, "", 0, "parse_error")))
        return metrics, _fresh_state(iface or "", pppoe_iface or ""), None

    state_path = _state_path(config)
    state = _load_state(state_path, iface, pppoe_iface)

    values, error = collect_ethernet(iface, sysfs_root=sysfs_root)
    pppoe = collect_pppoe(pppoe_iface, sysfs_root=sysfs_root, runner=runner)

    if error is not None:
        # Failed ethernet probe: emit no carrier/counter values at all and do
        # not touch the last good state file.
        metrics = {
            "ucg_probe_success": 0,
            "ucg_probe_error": error,
            "ucg_probe_baseline": 0,
            "ucg_probe_counter_reset": 0,
        }
        metrics.update(_pppoe_metrics(pppoe))
        return metrics, state, state_path

    metrics = compute_metrics(state, values, pppoe)
    return metrics, state, state_path


def run(config, sysfs_root=SYSFS_NET, runner=None):
    """Poll entry point: probe, persist state on success, print one JSON line."""
    metrics, state, state_path = probe(config, sysfs_root=sysfs_root, runner=runner)
    if metrics.get("ucg_probe_success") == 1 and state_path:
        _save_state(state_path, state)
    print(json.dumps(metrics))


if __name__ == "__main__":
    try:
        _cfg = json.load(sys.stdin)
    except ValueError:
        _cfg = {}
    if not isinstance(_cfg, dict):
        _cfg = {}
    try:
        run(_cfg)
    except Exception:
        # Never emit a raw exception message; the taxonomy is closed.
        print(
            json.dumps(
                {
                    "ucg_probe_success": 0,
                    "ucg_probe_error": "read_error",
                    "ucg_probe_baseline": 0,
                    "ucg_probe_counter_reset": 0,
                    "ucg_pppoe_up": 0,
                    "ucg_pppoe_ip": "",
                    "ucg_pppoe_query_success": 0,
                    "ucg_pppoe_query_error": "command_failed",
                }
            )
        )