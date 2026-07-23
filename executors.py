import json
import os
import subprocess
from typing import Any


def _load_executor_config() -> dict[str, Any]:
    fpath = os.path.join(os.path.dirname(__file__), "conf", "executors.json")
    try:
        with open(fpath, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


EXECUTOR_CONFIG: dict[str, Any] = _load_executor_config()


def _build_command(executor_id: str, rule: Any, agentid: str, metric: str, value: float, message: str) -> str | None:
    conf = EXECUTOR_CONFIG.get(executor_id)
    if not conf:
        return None
    command_template = conf.get("command")
    if not command_template:
        return None
    return command_template.format(
        rule_id=rule.id,
        agentid=agentid,
        pluginid=rule.pluginid,
        metric=metric,
        value=value,
        message=message,
        severity=rule.severity,
    )


def run_executors(
    rule: Any,
    agentid: str,
    metric: str,
    value: float,
    message: str,
) -> list[dict[str, str]]:
    """
    Run server-side executors and return agent-side executor commands.
    Each executor defines a shell command and an execution_target field.
    """
    agent_executors: list[dict[str, str]] = []

    if not rule.executors:
        return agent_executors

    for executor_id in rule.executors:
        conf = EXECUTOR_CONFIG.get(executor_id)
        if not conf or not conf.get("enabled", True):
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

        target = conf.get("execution_target", "server")

        if target == "agent":
            agent_executors.append({
                "id": executor_id,
                "command": command,
            })
        else:
            try:
                subprocess.run(command, shell=True, check=False)
            except Exception:
                continue

    return agent_executors