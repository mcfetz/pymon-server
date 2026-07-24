#!/usr/bin/env python3
"""cert_valid.py — TLS certificate expiry check. No external deps."""
import json
import socket
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit




__schema__ = {
    'label': 'TLS Certificate',
    'description': 'SSL certificate expiry check',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 86400, 'min': 300},
        {'key': 'timeout', 'label': 'Timeout per host (s)', 'type': 'number', 'default': 5, 'min': 1, 'max': 10},
        {'key': 'urls', 'label': 'HTTPS URLs', 'type': 'array:string', 'default': []},
    ],
}

MAX_PARALLEL_CHECKS = 10
MAX_PLUGIN_RUNTIME = 25


def metric_name(hostname: str) -> str:
    """Use the certificate target's domain as the metric identifier."""
    return f'{hostname}:remaining_days'


def check_certificate(url: str, timeout: int) -> tuple[str, float | None]:
    if not isinstance(url, str) or not url.startswith('https://'):
        return url, None

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        return url, None
    if not hostname:
        return url, None

    context = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
    except (OSError, ValueError):
        return url, None

    not_after = cert.get('notAfter')
    if not not_after:
        return url, None
    try:
        expires = datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
    except ValueError:
        return url, None

    days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400.0
    return metric_name(hostname), round(days, 1)

if __name__ == "__main__":
    config = json.load(sys.stdin)
    urls = config.get("urls", [])
    if not isinstance(urls, list):
        urls = []
    try:
        timeout = max(1, min(10, int(config.get('timeout', 5))))
    except (TypeError, ValueError):
        timeout = 5

    metrics = {}
    workers = min(MAX_PARALLEL_CHECKS, len(urls)) or 1
    batches = (len(urls) + workers - 1) // workers
    effective_timeout = min(timeout, max(1, MAX_PLUGIN_RUNTIME // batches - 1)) if urls else timeout
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for metric, days in executor.map(lambda target: check_certificate(str(target), effective_timeout), urls):
            if days is not None:
                metrics[metric] = days

    print(json.dumps(metrics))
