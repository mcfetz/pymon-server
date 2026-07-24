#!/usr/bin/env python3
"""http.py — HTTP health checks. Uses urllib (stdlib, no external deps)."""
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor




__schema__ = {
    'label': 'HTTP Check',
    'description': 'HTTP/HTTPS status code and content check',
    'fields': [
        {'key': 'sleep', 'label': 'Interval (s)', 'type': 'number', 'default': 60, 'min': 5},
        {'key': 'timeout', 'label': 'Timeout per URL (s)', 'type': 'number', 'default': 5, 'min': 1, 'max': 10},
        {'key': 'urls', 'label': 'URLs', 'type': 'array:object', 'default': [], 'fields': [
            {'key': 'name', 'label': 'Name', 'type': 'string'},
            {'key': 'url', 'label': 'URL', 'type': 'string'},
            {'key': 'expected_string', 'label': 'Expected text', 'type': 'string', 'optional': True},
        ]},
    ],
}

MAX_PARALLEL_CHECKS = 10
MAX_PLUGIN_RUNTIME = 25


def check_url(entry: dict, timeout: int) -> dict:
    if not isinstance(entry, dict):
        return {}
    name = str(entry.get('name') or '').strip()
    url = str(entry.get('url') or '').strip()
    expected = entry.get('expected_string')
    if not name or not url:
        return {}

    status_code = None
    content_ok = None
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            if expected is not None:
                body = resp.read().decode('utf-8', errors='replace')
                content_ok = 1 if str(expected) in body else 0
    except urllib.error.HTTPError as e:
        status_code = e.code
        if expected is not None:
            content_ok = 0
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        if expected is not None:
            content_ok = 0

    metrics = {}
    if status_code is not None:
        metrics[f'{name}:status_code'] = status_code
    if content_ok is not None:
        metrics[f'{name}:content_ok'] = content_ok
    return metrics

if __name__ == "__main__":
    config = json.load(sys.stdin)
    try:
        timeout = max(1, min(10, int(config.get('timeout', 5))))
    except (TypeError, ValueError):
        timeout = 5
    urls = config.get("urls", [])
    if not isinstance(urls, list):
        urls = []

    metrics = {}
    workers = min(MAX_PARALLEL_CHECKS, len(urls)) or 1
    batches = (len(urls) + workers - 1) // workers
    effective_timeout = min(timeout, max(1, MAX_PLUGIN_RUNTIME // batches - 1)) if urls else timeout
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(lambda entry: check_url(entry, effective_timeout), urls):
            metrics.update(result)

    print(json.dumps(metrics))
