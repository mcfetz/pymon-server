import json
import os
from datetime import UTC, datetime
from typing import Any

from config import CONF_DIR


SNOOZE_FILE = os.path.join(CONF_DIR, "snoozes.json")


def load_snoozes() -> list[dict[str, Any]]:
    try:
        with open(SNOOZE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_snoozes(snoozes: list[dict[str, Any]]) -> None:
    with open(SNOOZE_FILE, "w", encoding="utf-8") as f:
        json.dump(snoozes, f, indent=2)


def snooze_key(rule_id: str, agentid: str, pluginid: str, metric: str) -> str:
    return f"{rule_id}|{agentid}|{pluginid}|{metric}"


def _expires_at(snooze: dict[str, Any]) -> datetime | None:
    value = snooze.get("expires_at")
    if not value:
        return None
    try:
        expires_at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at


def prune_expired_snoozes(snoozes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Remove expired timed snoozes while keeping legacy permanent entries."""
    loaded = load_snoozes() if snoozes is None else snoozes
    now = datetime.now(UTC)
    active = []
    for snooze in loaded:
        expires_at = _expires_at(snooze)
        if not snooze.get("expires_at") or (expires_at is not None and expires_at > now):
            active.append(snooze)
    if len(active) != len(loaded):
        save_snoozes(active)
    return active


def is_snoozed(rule_id: str, agentid: str, pluginid: str, metric: str) -> bool:
    key = snooze_key(rule_id, agentid, pluginid, metric)
    return any(
        snooze_key(s["rule_id"], s["agentid"], s["pluginid"], s["metric"]) == key
        for s in prune_expired_snoozes()
    )
