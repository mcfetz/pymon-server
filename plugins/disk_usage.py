#!/usr/bin/env python3
"""disk_usage.py — Disk usage per mountpoint via os.statvfs. No external deps."""
import json, os, sys




__schema__ = {'label': 'Disk', 'description': 'Disk usage per mountpoint', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5}, {'key': 'excludes', 'label': 'Excluded mountpoints', 'type': 'array:string', 'default': []}]}

if __name__ == "__main__":
    config = json.load(sys.stdin)
    excludes = config.get("excludes", [])
    import re

    # Read mountpoints from /proc/mounts
    mountpoints = []
    with open("/proc/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mp = parts[1]
                # Skip pseudo-filesystems
                if mp.startswith(("/sys", "/proc", "/dev", "/run", "/var/run")):
                    continue
                mountpoints.append(mp)

    metrics = {}
    for mp in sorted(set(mountpoints)):
        if any(re.search(ex, mp) for ex in excludes):
            continue
        try:
            st = os.statvfs(mp)
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bfree
            percent = round(100.0 * (1.0 - free / total), 1) if total else 0.0
            metrics[mp] = percent
        except OSError:
            pass

    print(json.dumps(metrics))