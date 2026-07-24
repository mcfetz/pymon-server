#!/usr/bin/env python3
"""openrouter-costs.py — Remaining OpenRouter API key budget."""

import json
import sys
import urllib.error
import urllib.request


OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

__schema__ = {
    "label": "OpenRouter Costs",
    "description": "Remaining OpenRouter account budget for a regular API key",
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
            "type": "password",
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


def _account_remaining(data: dict) -> float | None:
    total_credits = data.get("total_credits")
    total_usage = data.get("total_usage")
    if total_credits is None or total_usage is None:
        return None
    try:
        return float(total_credits) - float(total_usage)
    except (TypeError, ValueError):
        return None


def _key_remaining(data: dict) -> float | None:
    remaining = data.get("limit_remaining")
    if remaining is None:
        return None
    try:
        return float(remaining)
    except (TypeError, ValueError):
        return None


def _fetch_json(url: str, api_key: str, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
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

    if not isinstance(payload, dict):
        raise RuntimeError("OpenRouter API returned an invalid response")
    return payload


def fetch_budget(api_key: str, timeout: int) -> float:
    """Return account credits remaining, constrained by a key limit if set."""
    credits_payload = _fetch_json(OPENROUTER_CREDITS_URL, api_key, timeout)
    credits_data = credits_payload.get("data")
    account_remaining = _account_remaining(credits_data) if isinstance(credits_data, dict) else None

    # A regular key may also have its own spending cap. Null means unlimited.
    try:
        key_payload = _fetch_json(OPENROUTER_KEY_URL, api_key, timeout)
        key_data = key_payload.get("data")
        key_remaining = _key_remaining(key_data) if isinstance(key_data, dict) else None
    except RuntimeError:
        key_remaining = None

    if account_remaining is not None and key_remaining is not None:
        return min(account_remaining, key_remaining)
    if account_remaining is not None:
        return account_remaining
    if key_remaining is not None:
        return key_remaining
    raise RuntimeError("OpenRouter API returned no remaining budget")


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
