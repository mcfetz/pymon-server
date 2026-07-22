#!/usr/bin/env python3
"""ram.py — RAM and swap usage via /proc/meminfo. No external deps."""
import json, sys




__schema__ = {'label': 'RAM', 'description': 'Memory and swap usage', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 30, 'min': 5}]}

if __name__ == "__main__":
    config = json.load(sys.stdin)

    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val_str = parts[1].strip().split()[0]
                meminfo[key] = int(val_str)

    total = meminfo.get("MemTotal", 1)
    available = meminfo.get("MemAvailable", 0)
    virtual_pct = round(100.0 * (1.0 - available / total), 1)

    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    swap_pct = round(100.0 * (1.0 - swap_free / swap_total), 1) if swap_total else 0.0

    print(json.dumps({"virtual_pct": virtual_pct, "swap_pct": swap_pct}))