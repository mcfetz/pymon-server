#!/usr/bin/env python3
"""homey_system.py - System metrics for a Homey. No external deps.

Two data sources, tried in order:

1. The collector bridge of the pymon Homey app (``PYMON_HOMEY_BRIDGE``). This is
   the only working source on a Homey: app processes run in a sandbox without
   ``/proc``, so the app itself asks the Homey Web API (``/manager/system/``)
   with an owner API token and passes the answer on. It carries CPU jiffies,
   core clock, core temperature, load averages, RAM, swap, storage and the
   wifi/ethernet state.
2. ``/proc`` directly, for the case where this plugin runs as an ordinary
   agent on a Linux host.

``data_source`` reports which one produced the numbers. CPU utilisation is the
average since the previous run, taken from the cumulative jiffy counters, so no
fixed sample window is assumed; on the first run two samples one second apart
are taken instead of reporting nothing.

Homey does not expose network byte counters, so rx/tx throughput is not
available here; the connection state that the Homey does report is.
"""
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

__schema__ = {
    "label": "Homey System",
    "description": "CPU, memory, swap, storage and network state of the Homey",
    "fields": [
        {"key": "sleep", "label": "Interval (s)", "type": "number", "default": 30, "min": 5},
    ],
}

BRIDGE_ENV = "PYMON_HOMEY_BRIDGE"
TOKEN_ENV = "PYMON_HOMEY_BRIDGE_TOKEN"

STATE_FILE = "/tmp/pymon_homey_system_state.json"

KIB = 1024
MIB = 1024 * 1024


def _bridge_get(path, timeout=5):
    """GET a collector bridge endpoint. Returns None when unavailable."""
    base = os.environ.get(BRIDGE_ENV)
    if not base:
        return None
    token = os.environ.get(TOKEN_ENV, "")
    request = urllib.request.Request(
        f"{base}{path}",
        headers={"X-Pymon-Bridge-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ── helpers ──────────────────────────────────────────────────────────────────


def _first_number(payload, keys):
    """Return the first of ``keys`` found in payload, or None."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _is_bridge_payload(payload):
    """True when the bridge answered with data rather than an error envelope.

    Deliberately does not look for any particular field: whether ``/system/info``
    carries ``cpus`` depends on the firmware and on which provider answers, and
    keying the decision on such a field would make the whole bridge look broken
    on an older Homey.
    """
    return isinstance(payload, dict) and "error" not in payload


def _unit_from_total(total):
    """Divisor that converts a Homey size payload into MB.

    Homey reports bytes on current firmware, but kilobytes are plausible on
    older builds. The unit has to be decided once from the total and then used
    for every value in the payload: deciding per value breaks as soon as one
    value is smaller than the 1 GiB threshold (a few hundred MiB of free memory
    would be read as kilobytes).
    """
    if total is not None and total >= 1024 ** 3:
        return MIB
    return KIB


def _to_mb(value, divisor):
    """Convert a size to MB using a divisor chosen by _unit_from_total."""
    if value is None:
        return None
    return round(value / divisor, 1)


def _bool_metric(value):
    """Homey reports booleans; pymon stores them as 1/0."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    return None


def _load_state():
    try:
        with open(STATE_FILE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w") as handle:
            json.dump(state, handle)
    except OSError:
        pass


# ── cpu ──────────────────────────────────────────────────────────────────────


def _cpu_times_from_info(info):
    """Aggregate the per-core jiffies of a Homey into a single counter set.

    ``/manager/system/`` reports ``cpus`` in the shape of Node's ``os.cpus()``:
    cumulative ``user``, ``nice``, ``sys``, ``idle`` and ``irq`` ticks per core.
    Returns None when the field is missing or empty.
    """
    cpus = (info or {}).get("cpus")
    if not isinstance(cpus, list) or not cpus:
        return None

    totals = {}
    for core in cpus:
        times = core.get("times") if isinstance(core, dict) else None
        if not isinstance(times, dict):
            return None
        for key, value in times.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value

    idle = totals.get("idle", 0)
    busy_total = sum(v for k, v in totals.items() if k != "idle")
    return {"idle": idle, "total": idle + busy_total}


def _cpu_times_from_proc():
    """Return (idle, iowait, steal, legacy_total, full_total) from /proc/stat."""
    with open("/proc/stat") as handle:
        line = handle.readline()
    parts = [int(value) for value in line.split()[1:]]
    idle = parts[3]
    iowait = parts[4] if len(parts) > 4 else 0
    steal = parts[7] if len(parts) > 7 else 0
    return idle, iowait, steal, sum(parts[:7]), sum(parts[:8])


def _delta_percent(before, after):
    """Utilisation from two cumulative counter sets."""
    idle_delta = after["idle"] - before["idle"]
    total_delta = after["total"] - before["total"]
    if total_delta <= 0:
        return None
    return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def _cpu_metrics_bridge(info, sample_again):
    """CPU utilisation from the bridge counters, plus identity and clock."""
    metrics = {}
    cpus = info.get("cpus") if isinstance(info, dict) else None
    cpus = cpus if isinstance(cpus, list) else []

    first = cpus[0] if cpus and isinstance(cpus[0], dict) else {}
    if first.get("model"):
        metrics["cpu_model"] = first["model"]
    if isinstance(first.get("speed"), (int, float)):
        metrics["cpu_mhz"] = round(float(first["speed"]), 1)
    if cpus:
        metrics["cpu_count"] = len(cpus)

    clock = _first_number(info, ("videoCoreClock",))
    if clock:
        metrics["cpu_clock_mhz"] = round(clock / 1e6, 1)

    # No jiffies means no utilisation figure, but the identity metrics above
    # are still worth reporting.
    current = _cpu_times_from_info(info)
    if current is None:
        return metrics

    state = _load_state()
    previous = state.get("cpu")
    now = time.time()

    if isinstance(previous, dict):
        percent = _delta_percent(
            {"idle": previous.get("idle", 0), "total": previous.get("total", 0)},
            current,
        )
        if percent is not None:
            metrics["cpu_percent"] = percent

    _save_state({"cpu": current, "cpu_time": now})

    if "cpu_percent" not in metrics and sample_again:
        # First run, or the stored sample is unusable: take a second one.
        time.sleep(1)
        second_info = _bridge_get("/system/info")
        second = _cpu_times_from_info(second_info)
        if second is not None:
            percent = _delta_percent(current, second)
            if percent is not None:
                metrics["cpu_percent"] = percent
            _save_state({"cpu": second, "cpu_time": time.time()})

    return metrics


def _cpu_metrics_proc():
    """CPU utilisation from /proc/stat, for a plain Linux host."""
    try:
        before = _cpu_times_from_proc()
        time.sleep(1)
        after = _cpu_times_from_proc()
    except (OSError, IndexError, ValueError):
        return {}

    idle_delta = after[0] - before[0]
    iowait_delta = after[1] - before[1]
    steal_delta = after[2] - before[2]
    legacy_delta = after[3] - before[3]
    full_delta = after[4] - before[4]

    metrics = {"cpu_count": os.cpu_count() or 0}
    if legacy_delta <= 0:
        metrics.update({"cpu_percent": 0.0, "cpu_iowait_pct": 0.0, "cpu_steal_pct": 0.0})
        return metrics

    metrics.update(
        {
            "cpu_percent": round(100.0 * (1.0 - idle_delta / legacy_delta), 1),
            "cpu_iowait_pct": round(100.0 * iowait_delta / legacy_delta, 1),
            "cpu_steal_pct": round(100.0 * steal_delta / full_delta, 1) if full_delta else 0.0,
        }
    )
    return metrics


# ── memory, storage, network ─────────────────────────────────────────────────


def _memory_metrics(bridge_memory, info):
    """RAM and swap, from the bridge if possible, else /proc/meminfo."""
    if isinstance(bridge_memory, dict) and "total" in bridge_memory:
        total_raw = _first_number(bridge_memory, ("total",))
        divisor = _unit_from_total(total_raw)
        total_mb = _to_mb(total_raw, divisor)
        free_mb = _to_mb(_first_number(bridge_memory, ("free",)), divisor)
        metrics = {}
        if total_mb is not None:
            metrics["mem_total_mb"] = total_mb
        if free_mb is not None:
            metrics["mem_free_mb"] = free_mb
            if total_mb:
                metrics["mem_used_mb"] = round(total_mb - free_mb, 1)
                metrics["mem_percent"] = round(100.0 * (total_mb - free_mb) / total_mb, 1)
        # Homey reports swap as a single figure whose total-vs-used meaning
        # depends on firmware, so it is published unscaled and unlabelled.
        swap_mb = _to_mb(_first_number(bridge_memory, ("swap",)), divisor)
        if swap_mb is not None:
            metrics["swap_mb"] = swap_mb
        return metrics

    try:
        with open("/proc/meminfo") as handle:
            meminfo = {}
            for line in handle:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return {}

    total_mb = meminfo.get("MemTotal", 0) / KIB
    available_mb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) / KIB
    used_mb = total_mb - available_mb
    metrics = {
        "mem_total_mb": round(total_mb, 1),
        "mem_free_mb": round(available_mb, 1),
        "mem_used_mb": round(used_mb, 1),
        "mem_percent": round(100.0 * used_mb / total_mb, 1) if total_mb else 0.0,
    }
    swap_total_mb = meminfo.get("SwapTotal", 0) / KIB
    swap_free_mb = meminfo.get("SwapFree", 0) / KIB
    if swap_total_mb:
        metrics["swap_mb"] = round(swap_total_mb - swap_free_mb, 1)
    return metrics


def _storage_metrics(bridge_storage):
    """Disk usage, from the bridge if possible, else the local filesystem."""
    if isinstance(bridge_storage, dict) and "total" in bridge_storage:
        total_raw = _first_number(bridge_storage, ("total",))
        divisor = _unit_from_total(total_raw)
        total_mb = _to_mb(total_raw, divisor)
        free_mb = _to_mb(_first_number(bridge_storage, ("free",)), divisor)
        metrics = {}
        if total_mb is not None:
            metrics["storage_total_mb"] = total_mb
        if free_mb is not None:
            metrics["storage_free_mb"] = free_mb
            if total_mb:
                metrics["storage_used_mb"] = round(total_mb - free_mb, 1)
                metrics["storage_percent"] = round(100.0 * (total_mb - free_mb) / total_mb, 1)
        return metrics

    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return {}
    total_mb = usage.total / MIB
    used_mb = usage.used / MIB
    return {
        "storage_total_mb": round(total_mb, 1),
        "storage_free_mb": round(usage.free / MIB, 1),
        "storage_used_mb": round(used_mb, 1),
        "storage_percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
    }


def _network_metrics(info):
    """Connection state as the Homey reports it.

    The Homey exposes no interface byte counters, so throughput cannot be
    derived from this source.
    """
    if not isinstance(info, dict):
        return {}

    metrics = {}
    for interface in ("wifi", "ethernet"):
        connected = info.get(f"{interface}Connected")
        if isinstance(connected, bool):
            metrics[f"{interface}_connected"] = 1 if connected else 0
        for source, name in (
            ("Address", "address"),
            ("Mac", "mac"),
        ):
            value = info.get(f"{interface}{source}")
            if isinstance(value, str) and value:
                metrics[f"{interface}_{name}"] = value

    ssid = info.get("wifiSsid")
    if isinstance(ssid, str) and ssid:
        metrics["wifi_ssid"] = ssid
    for source, name in (("wifiStrength", "wifi_strength"), ("wifiFrequency", "wifi_frequency")):
        value = _first_number(info, (source,))
        if value is not None:
            metrics[name] = value

    return metrics


def _core_metrics(info):
    """Identity, uptime, load and the SoC health flags."""
    if not isinstance(info, dict):
        return {}

    metrics = {}
    for source, name in (
        ("homeyVersion", "homey_version"),
        ("homeyModelName", "homey_model"),
        ("hostname", "hostname"),
        ("platform", "platform"),
        ("rebootReason", "reboot_reason"),
    ):
        value = info.get(source)
        if isinstance(value, str) and value:
            metrics[name] = value

    uptime = _first_number(info, ("uptime",))
    if uptime is not None:
        metrics["uptime_s"] = round(uptime, 1)

    loadavg = info.get("loadavg")
    if isinstance(loadavg, list) and loadavg:
        for index, label in enumerate(("load_1min", "load_5min", "load_15min")):
            if index < len(loadavg) and isinstance(loadavg[index], (int, float)):
                metrics[label] = round(float(loadavg[index]), 2)

    temperature = _first_number(info, ("videoCoreTemperature",))
    if temperature is not None:
        metrics["cpu_temp_c"] = round(temperature, 1)

    for source, name in (
        ("videoCoreUnderVoltageCurrently", "cpu_undervoltage"),
        ("videoCoreThrottleCurrently", "cpu_throttled"),
        ("videoCoreSoftTemperatureLimitActiveCurrently", "cpu_temp_limit_active"),
        ("videoCoreArmFrequencyCappedCurrently", "cpu_freq_capped"),
    ):
        flag = _bool_metric(info.get(source))
        if flag is not None:
            metrics[name] = flag

    return metrics


def _uptime():
    try:
        with open("/proc/uptime") as handle:
            return round(float(handle.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


if __name__ == "__main__":
    json.load(sys.stdin)

    bridge_info = _bridge_get("/system/info")
    bridge_memory = _bridge_get("/system/memory")
    bridge_storage = _bridge_get("/system/storage")
    bridge_active = 1 if _is_bridge_payload(bridge_info) else 0

    metrics = {"bridge_available": bridge_active}

    if bridge_active:
        metrics["data_source"] = "homey_web_api"
        metrics.update(_cpu_metrics_bridge(bridge_info, sample_again=True))
    else:
        # No bridge: either not running on a Homey app, or the owner session is
        # not available. Fall back to the host's own /proc.
        metrics["data_source"] = "proc"
        metrics.update(_cpu_metrics_proc())
        proc_uptime = _uptime()
        if proc_uptime is not None:
            metrics["uptime_s"] = proc_uptime
        sys.stderr.write(
            "collector bridge unavailable; read /proc on this host instead\n"
        )

    metrics.update(_memory_metrics(bridge_memory, None))
    metrics.update(_storage_metrics(bridge_storage))
    if bridge_active:
        metrics.update(_network_metrics(bridge_info))
        metrics.update(_core_metrics(bridge_info))

    print(json.dumps(metrics))