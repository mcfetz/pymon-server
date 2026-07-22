#!/usr/bin/env python3
"""services.py — systemd service status. No external deps."""
import json, subprocess, sys




__schema__ = {'label': 'Services', 'description': 'systemd service status check', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5}, {'key': 'services', 'label': 'Service names', 'type': 'array:string', 'default': []}]}

if __name__ == "__main__":
    config = json.load(sys.stdin)
    services = config.get("services", [])

    if not sys.platform.startswith("linux"):
        print(json.dumps({}))
        sys.exit(0)

    def _is_active(svc):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=10,
            )
            return r.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    if not services:
        # List all systemd services
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
                capture_output=True, text=True, timeout=15,
            )
            services = [line.split()[0] for line in r.stdout.splitlines() if line.strip() and ".service" in line]
        except Exception:
            print(json.dumps({}))
            sys.exit(0)

    metrics = {svc: _is_active(svc) for svc in services}
    print(json.dumps(metrics))