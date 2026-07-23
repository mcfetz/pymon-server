import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def _load_executors_fresh():
    fpath = os.path.join(os.path.dirname(__file__), "conf", "executors.json")
    try:
        with open(fpath, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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
    agent_executors: list[dict[str, str]] = []

    if not rule.executors:
        return agent_executors

    config = _load_executors_fresh()

    for executor_id in rule.executors:
        conf = config.get(executor_id)
        if not conf:
            logger.warning("Executor '%s' not found in config", executor_id)
            continue
        if not conf.get("enabled", True):
            logger.info("Executor '%s' is disabled", executor_id)
            continue

        command_template = conf.get("command")
        if not command_template:
            logger.warning("Executor '%s' has no command", executor_id)
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
        logger.info("Executor '%s' target=%s cmd=%s", executor_id, target, command)

        if target == "agent":
            agent_executors.append({"id": executor_id, "command": command})
        else:
            try:
                subprocess.run(command, shell=True, check=False)
            except Exception:
                continue

    return agent_executors