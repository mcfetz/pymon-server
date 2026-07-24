#!/usr/bin/env python3
"""openrouter-costs.py — Remaining OpenRouter API key budget."""

import json
import sys
import urllib.error
import urllib.request


OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

__schema__ = {
    "label": "OpenRouter Costs",
    "description": "Remaining budget for a regular OpenRouter API key",
    "fields": [
        {
            "key": "sleep",
            "label": "Interval (s)",
            "type": "number",
            "default": 300,
            "min": 60,
        },
        {
            "key": "api_key",
            "label": "OpenRouter API key (not management key)",
            "type": "string",
        },
        {
            "key": "timeout",
            "label": "Timeout (s)",
            "type": "number",
            "default": 10,
            "min": 1,
            "max": 30,
        },
    ],
}


def _remaining_budget(data: dict) -> float:
    remaining = data.get("limit_remaining")
    if remaining is None:
        limit = data.get("limit")
        usage = data.get("usage")
        if limit is not None and usage is not None:
            try:
                remaining = float(limit) - float(usage)
            except (TypeError, ValueError):
                remaining = None

    # OpenRouter returns null when the key has no finite credit limit.
    return -1.0 if remaining is None else float(remaining)


def fetch_budget(api_key: str, timeout: int) -> float:
    request = urllib.request.Request(
        OPENROUTER_KEY_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "pymon-openrouter-costs",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"OpenRouter API returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise RuntimeError("OpenRouter API request failed") from error

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("OpenRouter API returned an invalid response")
    return _remaining_budget(data)


if __name__ == "__main__":
    config = json.load(sys.stdin)
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        print("OpenRouter API key is missing", file=sys.stderr)
        sys.exit(1)

    try:
        timeout = max(1, min(30, int(config.get("timeout", 10))))
    except (TypeError, ValueError):
        timeout = 10

    try:
        budget = fetch_budget(api_key, timeout)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"budget_remaining": round(budget, 6)}))
