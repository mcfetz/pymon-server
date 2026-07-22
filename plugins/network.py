#!/usr/bin/env python3
"""network.py — Network throughput via /proc/net/dev. No external deps."""
import json, sys, time, os




__schema__ = {'label': 'Network', 'description': 'Network I/O and TCP connections', 'fields': [{'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 30, 'min': 5}]}

STATE_FILE = "/tmp/pymon_network_state.json"

if __name__ == "__main__":
    config = json.load(sys.stdin)

    counters = {}
    with open("/proc/net/dev") as f:
        f.readline()  # header
        f.readline()
        for line in f:
            parts = line.strip().split()
            if not parts:
                continue
            ifname = parts[0].rstrip(":")
            rx_bytes = int(parts[1])
            tx_bytes = int(parts[9])
            counters[ifname] = (rx_bytes, tx_bytes)

    metrics = {}
    now = time.time()

    # Load previous state for rate calculation
    prev_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                prev_state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    prev_counters = prev_state.get("counters", {})
    prev_time = prev_state.get("time", now)

    for ifname, (rx, tx) in counters.items():
        metrics[f"{ifname}:bytes_recv"] = rx
        metrics[f"{ifname}:bytes_sent"] = tx

        if ifname in prev_counters:
            dt = now - prev_time
            if dt > 0:
                prev_rx, prev_tx = prev_counters[ifname]
                metrics[f"{ifname}:rx_bytes_per_sec"] = round((rx - prev_rx) / dt, 1)
                metrics[f"{ifname}:tx_bytes_per_sec"] = round((tx - prev_tx) / dt, 1)

    # Save current state
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"counters": {k: list(v) for k, v in counters.items()}, "time": now}, f)
    except OSError:
        pass

    # TCP connections count
    try:
        with open("/proc/net/tcp") as f:
            tcp_count = sum(1 for line in f if line.strip() and not line.startswith("  sl"))
        metrics["tcp_open_connections"] = tcp_count
    except OSError:
        pass

    print(json.dumps(metrics))