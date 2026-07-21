#!/usr/bin/env python3
"""cpu.py — CPU usage via /proc/stat. No external deps."""
import json, sys, time

def _cpu_times():
    with open("/proc/stat") as f:
        line = f.readline()
    parts = line.split()
    fields = [int(v) for v in parts[1:8]]
    idle = fields[3]
    total = sum(fields)
    return idle, total

if __name__ == "__main__":
    config = json.load(sys.stdin)

    prev_idle, prev_total = _cpu_times()
    time.sleep(1)
    idle, total = _cpu_times()

    idle_delta = idle - prev_idle
    total_delta = total - prev_total
    percent = 100.0 * (1.0 - idle_delta / total_delta) if total_delta else 0.0

    print(json.dumps({"percent": round(percent, 1)}))