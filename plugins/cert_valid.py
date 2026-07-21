#!/usr/bin/env python3
"""cert_valid.py — TLS certificate expiry check. No external deps."""
import json, socket, ssl, sys
from datetime import datetime, timezone

if __name__ == "__main__":
    config = json.load(sys.stdin)
    urls = config.get("urls", [])
    timeout = int(config.get("timeout", 5))

    metrics = {}
    for url in urls:
        if not url.startswith("https://"):
            continue
        hostname = url.removeprefix("https://").split("/", 1)[0]
        context = ssl.create_default_context()
        try:
            with socket.create_connection((hostname, 443), timeout=timeout) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    cert = ssock.getpeercert()
        except OSError:
            continue

        not_after = cert.get("notAfter")
        if not not_after:
            continue
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400.0
        metrics[url] = round(days, 1)

    print(json.dumps(metrics))