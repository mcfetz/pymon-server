#!/usr/bin/env python3
"""cpu.py — CPU usage via /proc/stat. No external deps.

Reports total busy percent plus separate iowait and steal shares. The
``percent`` value keeps the original definition (idle share), so existing
rules stay valid.
"""
import json, sys, time


__schema__ = {'label': 'CPU', 'description': 'CPU usage, iowait and steal in percent', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 30, 'min': 5}]}


def _cpu_times():
    """Return (idle, iowait, steal, legacy_total, full_total) jiffies.

    legacy_total excludes steal so the ``percent`` metric keeps its original
    value; it is the guest-visible CPU window (user..softirq). steal is only
    consumed by the hypervisor, so steal_pct uses a denominator that includes
    it (mpstat semantics).
    """
    with open("/proc/stat") as f:
        line = f.readline()
    p = [int(v) for v in line.split()[1:]]
    idle = p[3]
    iowait = p[4] if len(p) > 4 else 0
    steal = p[7] if len(p) > 7 else 0
    legacy_total = sum(p[:7])
    full_total = sum(p[:8])
    return idle, iowait, steal, legacy_total, full_total


if __name__ == "__main__":
    config = json.load(sys.stdin)

    prev = _cpu_times()
    time.sleep(1)
    idle, iowait, steal, legacy_total, full_total = _cpu_times()

    idle_delta = idle - prev[0]
    iowait_delta = iowait - prev[1]
    steal_delta = steal - prev[2]
    legacy_delta = legacy_total - prev[3]
    full_delta = full_total - prev[4]

    if legacy_delta:
        percent = round(100.0 * (1.0 - idle_delta / legacy_delta), 1)
        iowait_pct = round(100.0 * iowait_delta / legacy_delta, 1)
    else:
        percent = iowait_pct = 0.0
    steal_pct = round(100.0 * steal_delta / full_delta, 1) if full_delta else 0.0

    print(json.dumps({
        "percent": percent,
        "iowait_pct": iowait_pct,
        "steal_pct": steal_pct,
    }))