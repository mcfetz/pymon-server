import json
import os
import subprocess
from typing import Any

import toml


def load_executor_config(path: str = "conf/executors.toml") -> dict[str, Any]:
    try:
        data = toml.load(path)
    except FileNotFoundError:
        return {}
    return data.get("executors", {})


EXECUTOR_CONFIG: dict[str, Any] = load_executor_config()


def _is_executor_enabled(exec_id: str) -> bool:
    exec_json = os.path.join(os.path.dirname(__file__), "conf", "executors.json")
    try:
        with open(exec_json, encoding="utf-8") as f:
            cfg = json.load(f)
        entry = cfg.get(exec_id, {})
        return entry.get("enabled", True)
    except Exception:
        return True


def run_executors(
    rule: Any,
    agentid: str,
    metric: str,
    value: float,
    message: str,
) -> None:
    """
    Run all configured executors for a rule.
    Each executor defines a shell command in conf/executors.toml.
    """
    if not rule.executors:
        return

    for executor_id in rule.executors:
        if not _is_executor_enabled(executor_id):
            continue
        conf = EXECUTOR_CONFIG.get(executor_id)
        if not conf:
            continue

        command_template = conf.get("command")
        if not command_template:
            continue

        command = command_template.format(
            rule_id=rule.id,
            agentid=agentid,
            pluginid=rule.pluginid,
            metric=metric,
            value=value,
            message=message,
            severity=rule.severity,
        )

        try:
            subprocess.run(
                command,
                shell=True,
                check=False,
            )
        except Exception:
            # Fehler beim Executor sollen die restliche Verarbeitung nicht stoppen
            continue
