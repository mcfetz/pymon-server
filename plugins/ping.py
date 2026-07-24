#!/usr/bin/env python3
"""ping.py — ICMP ping to configured hosts. Uses subprocess. No external deps."""
import json
import platform
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor




__schema__ = {
    'label': 'Ping',
    'description': 'ICMP ping to configured hosts',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5},
        {'key': 'hosts', 'label': 'Hosts', 'type': 'array:string', 'default': []},
        {'key': 'timeout', 'label': 'Timeout per host (s)', 'type': 'number', 'default': 5, 'min': 1, 'max': 10},
    ],
}

MAX_PARALLEL_PINGS = 10
MAX_PLUGIN_RUNTIME = 25


def ping_host(host: str, timeout: int) -> tuple[str, bool, float | None]:
    success = False
    avg_time = None
    system = platform.system().lower()
    param = '-n' if system == 'windows' else '-c'
    command = ['ping', param, '4', host]
    if system == 'windows':
        command.extend(['-w', str(timeout * 1000)])
    else:
        command.extend(['-W', str(timeout), '-w', str(timeout)])

    try:
        output = subprocess.check_output(
            command,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout + 1,
        )
        success = True
        if system == 'windows':
            match = re.search(r'Durchschnitt = (\d+)ms', output)
            if match:
                avg_time = float(match.group(1))
        else:
            match = re.search(r'rtt [\w/]+ = [\d.]+/([\d.]+)/', output)
            if match:
                avg_time = float(match.group(1))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass
    return host, success, avg_time

if __name__ == "__main__":
    config = json.load(sys.stdin)
    hosts = config.get("hosts", [])
    if not isinstance(hosts, list):
        hosts = []
    try:
        timeout = max(1, min(10, int(config.get('timeout', 5))))
    except (TypeError, ValueError):
        timeout = 5
    results = {}

    workers = min(MAX_PARALLEL_PINGS, len(hosts)) or 1
    batches = (len(hosts) + workers - 1) // workers
    effective_timeout = min(timeout, max(1, MAX_PLUGIN_RUNTIME // batches - 1)) if hosts else timeout
    if effective_timeout < timeout:
        print(
            json.dumps({"warning": f"ping timeout reduced to {effective_timeout}s for {len(hosts)} hosts"}),
            file=sys.stderr,
        )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        ping_results = executor.map(lambda host: ping_host(str(host), effective_timeout), hosts)
        for host, success, avg_time in ping_results:
            results[f"{host} success"] = success
            results[f"{host} avg-time"] = avg_time

    print(json.dumps(results))
