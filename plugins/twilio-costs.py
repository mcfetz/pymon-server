#!/usr/bin/env python3
"""twilio-costs.py — Remaining Twilio account balance."""

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


TWILIO_BALANCE_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json"

__schema__ = {
    "label": "Twilio Costs",
    "description": "Remaining balance for a Twilio account",
    "fields": [
        {
            "key": "sleep",
            "label": "Interval (s)",
            "type": "number",
            "default": 300,
            "min": 60,
        },
        {
            "key": "sid",
            "label": "Twilio Account SID",
            "type": "string",
        },
        {
            "key": "client_secret",
            "label": "Twilio client secret / auth token",
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


def fetch_balance(sid: str, client_secret: str, timeout: int) -> float:
    sid = sid.strip()
    account_path = urllib.parse.quote(sid, safe="")
    credentials = base64.b64encode(f"{sid}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        TWILIO_BALANCE_URL.format(sid=account_path),
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
            "User-Agent": "pymon-twilio-costs",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError(
                "Twilio authentication or permission denied; use an Account SID/Auth Token "
                "or a Main API key with the Account SID"
            ) from error
        raise RuntimeError(f"Twilio API returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise RuntimeError("Twilio API request failed") from error

    try:
        balance = payload.get("balance", payload.get("account_balance"))
        return float(Decimal(str(balance)))
    except (KeyError, TypeError, InvalidOperation, ValueError) as error:
        raise RuntimeError("Twilio API returned an invalid balance") from error


if __name__ == "__main__":
    config = json.load(sys.stdin)
    sid = str(config.get("sid") or "").strip()
    client_secret = str(config.get("client_secret") or "").strip()
    if not sid or not client_secret:
        print("Twilio SID and client secret are required", file=sys.stderr)
        sys.exit(1)

    try:
        timeout = max(1, min(30, int(config.get("timeout", 10))))
    except (TypeError, ValueError):
        timeout = 10

    try:
        balance = fetch_balance(sid, client_secret, timeout)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"budget_remaining": round(balance, 6)}))
