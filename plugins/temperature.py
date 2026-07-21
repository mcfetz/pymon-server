#!/usr/bin/env python3
"""temperature.py — Hardware sensor temperatures via /sys/class/thermal. No external deps."""
import json, os, sys

if __name__ == "__main__":
    config = json.load(sys.stdin)

    metrics = {}
    thermal_base = "/sys/class/thermal"
    if not os.path.exists(thermal_base):
        print(json.dumps(metrics))
        sys.exit(0)

    try:
        for entry in sorted(os.listdir(thermal_base)):
            if not entry.startswith("thermal_zone"):
                continue
            type_path = os.path.join(thermal_base, entry, "type")
            temp_path = os.path.join(thermal_base, entry, "temp")
            if os.path.exists(type_path) and os.path.exists(temp_path):
                with open(type_path) as f:
                    zone_type = f.read().strip()
                with open(temp_path) as f:
                    raw = f.read().strip()
                celsius = int(raw) / 1000.0
                key = f"{zone_type}:{entry}" if zone_type else entry
                metrics[key] = round(celsius, 1)
    except OSError:
        pass

    print(json.dumps(metrics))