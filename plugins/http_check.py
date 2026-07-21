#!/usr/bin/env python3
"""http.py — HTTP health checks. Uses urllib (stdlib, no external deps)."""
import json, sys, urllib.request, urllib.error

if __name__ == "__main__":
    config = json.load(sys.stdin)
    timeout = int(config.get("timeout", 5))
    urls = config.get("urls", [])

    metrics = {}
    for entry in urls:
        name = entry.get("name")
        url = entry.get("url")
        expected = entry.get("expected_string")
        if not name or not url:
            continue

        status_code = None
        content_ok = None
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status_code = resp.status
                if expected is not None:
                    body = resp.read().decode("utf-8", errors="replace")
                    content_ok = 1 if expected in body else 0
        except urllib.error.HTTPError as e:
            status_code = e.code
        except Exception:
            if expected is not None:
                content_ok = 0

        if status_code is not None:
            metrics[f"{name}:status_code"] = status_code
        if content_ok is not None:
            metrics[f"{name}:content_ok"] = content_ok

    print(json.dumps(metrics))