#!/usr/bin/env python3
"""ping.py — ICMP ping to configured hosts. Uses subprocess. No external deps."""
import json, subprocess, sys, platform, re




__schema__ = {'label': 'Ping', 'description': 'ICMP ping to configured hosts', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5}, {'key': 'hosts', 'label': 'Hosts', 'type': 'array:string', 'default': []}]}

if __name__ == "__main__":
    config = json.load(sys.stdin)
    hosts = config.get("hosts", [])
    results = {}
    param = "-n" if platform.system().lower() == "windows" else "-c"
    count = "4"

    for host in hosts:
        success = False
        avg_time = None
        try:
            output = subprocess.check_output(
                ["ping", param, count, host],
                stderr=subprocess.STDOUT, text=True, timeout=30,
            )
            success = True
            if platform.system().lower() == "windows":
                m = re.search(r"Durchschnitt = (\d+)ms", output)
                if m:
                    avg_time = float(m.group(1))
            else:
                m = re.search(r"rtt [\w/]+ = [\d\.]+/([\d\.]+)/", output)
                if m:
                    avg_time = float(m.group(1))
        except subprocess.CalledProcessError:
            pass
        results[f"{host} success"] = success
        results[f"{host} avg-time"] = avg_time

    print(json.dumps(results))