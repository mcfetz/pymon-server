import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Literal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from db_models import Alarm, Metrics
from functions import get_value_from_row
from notifications import notify_targets
from cache import timed_cache
from executors import run_executors
from config import CONF_DIR
from snooze import is_snoozed

logger = logging.getLogger(__name__)

PostCommitAction = Callable[[], list[dict[str, str]]]

Condition = Literal["gt", "lt", "ge", "le", "eq", "ne", "between", "outside", "no_data"]
Scope = Literal["single", "moving_avg", "count_ratio", "change"]
FireMode = Literal["single", "multi", "replace"]
AgentsMode = Literal["exclude", "include"]


@dataclass
class Rule:
    id: str
    title: str
    enabled: bool
    description: str
    pluginid: str
    metric: str
    condition: Condition
    threshold: float | str   # float or "$VARNAME" reference
    scope: Scope
    window_size: int | None = None
    min_violations: int | None = None
    severity: str = "warning"
    notifications: list[str] | None = None
    fire: FireMode = "single"
    executors: list[str] | None = None
    agents: list[str] | None = None
    agents_mode: AgentsMode = "exclude"
    auto_close: bool = False
    threshold_min: float | str | None = None
    threshold_max: float | str | None = None


def _safe_float(val) -> float | str:
    """Return val as float, or as-is if it's a variable reference ($VARNAME)."""
    if isinstance(val, str) and val.startswith("$"):
        return val  # preserve variable reference
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_range_threshold(val) -> float | str | None:
    if val is None or val == "":
        return None
    return _safe_float(val)


@timed_cache(ttl_seconds=5)
def load_rules(path: str = "") -> list[Rule]:
    fpath = path or os.path.join(CONF_DIR, "rules.json")
    if not os.path.exists(fpath):
        return []
    with open(fpath, encoding="utf-8") as f:
        raw = json.load(f)
    rules: list[Rule] = []
    for rule_id, r in raw.items():
        rules.append(
            Rule(
                id=rule_id,
                title=r.get("title", ""),
                enabled=r.get("enabled", True),
                description=r.get("description", ""),
                pluginid=r.get("pluginid", ""),
                metric=r.get("metric", ""),
                condition=r.get("condition", "gt"),
                threshold=_safe_float(r.get("threshold", 0)),
                scope=r.get("scope", "single"),
                window_size=r.get("window_size"),
                min_violations=r.get("min_violations"),
                severity=r.get("severity", "warning"),
                notifications=r.get("notifications", []),
                fire=r.get("fire", "single"),
                executors=r.get("executors", []),
                agents=r.get("agents", []),
                agents_mode=r.get("agents_mode", "exclude"),
                auto_close=r.get("auto_close", False),
                threshold_min=_safe_range_threshold(r.get("threshold_min")),
                threshold_max=_safe_range_threshold(r.get("threshold_max")),
            )
        )
    return rules


def compare(
    value: float,
    condition: Condition,
    threshold: float,
    threshold_max: float | None = None,
) -> bool:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            logger.warning("compare: cannot convert string '%s' to float, skipping", value)
            return False
    if condition == "gt":
        return value > threshold
    if condition == "ge":
        return value >= threshold
    if condition == "lt":
        return value < threshold
    if condition == "le":
        return value <= threshold
    if condition == "eq":
        return value == threshold
    if condition == "ne":
        return value != threshold
    if condition == "between":
        return threshold_max is not None and threshold <= value <= threshold_max
    if condition == "outside":
        return threshold_max is not None and (value < threshold or value > threshold_max)
    logger.error("compare: unknown condition '%s' in rule evaluation", condition)
    return False


def compare_rule_value(value: float, rule: Rule, agentid: str) -> bool:
    """Compare a value using a single threshold or an inclusive range."""
    if rule.condition in ("between", "outside"):
        if rule.threshold_min is None or rule.threshold_max is None:
            logger.warning("rule '%s' has incomplete range thresholds", rule.id)
            return False
        threshold_min = _resolve_threshold(rule.threshold_min, agentid)
        threshold_max = _resolve_threshold(rule.threshold_max, agentid)
        if threshold_min > threshold_max:
            logger.warning("rule '%s' has threshold_min greater than threshold_max", rule.id)
            return False
        return compare(value, rule.condition, threshold_min, threshold_max)

    threshold = _resolve_threshold(rule.threshold, agentid)
    return compare(value, rule.condition, threshold)


@lru_cache(maxsize=256)
def _compile_metric_pattern(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error as e:
        logger.warning("invalid metric regex '%s': %s", pattern, e)
        return None


def metric_matches(pattern: str, metric: str) -> bool:
    """Match an exact metric name, wildcard, or full-name regex pattern."""
    if pattern == "*" or pattern == metric:
        return True
    if not isinstance(pattern, str) or not pattern:
        return False
    compiled = _compile_metric_pattern(pattern)
    return compiled is not None and compiled.fullmatch(metric) is not None


def has_open_alarm(session: Session, agentid: str, rule: Rule) -> bool:
    q = (
        select(Alarm)
        .where(
            Alarm.agentid == agentid,
            Alarm.rule_id == rule.id,
            Alarm.acknowledged == False,  # noqa: E712
        )
        .limit(1)
    )
    return session.execute(q).scalars().first() is not None


BLACKOUTS_FILE = os.path.join(CONF_DIR, "blackouts.json")
VARIABLES_FILE = os.path.join(CONF_DIR, "variables.json")


def _load_variables_fresh() -> dict:
    try:
        with open(VARIABLES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _resolve_threshold(threshold: float | str, agentid: str) -> float:
    """Resolve a threshold value: return float directly, or resolve $VARNAME.

    Priority for variable exceptions:
      1. Explicit agent exception
      2. Group exception (first matching group)
      3. Default variable value
    """
    if not isinstance(threshold, str) or not threshold.startswith("$"):
        return float(threshold)

    variables = _load_variables_fresh()
    var = next((v for v in variables.values() if v.get("name") == threshold), None)
    if var is None:
        logger.error("Variable '%s' not found — using 0 as threshold", threshold)
        return 0.0

    exceptions = var.get("exceptions", [])

    # 1. Agent-level exception (highest priority)
    for exc in exceptions:
        if exc.get("type") == "agent" and exc.get("id") == agentid:
            return float(exc["value"])

    # 2. Group-level exception
    agent_groups = _get_agent_groups(agentid)
    for exc in exceptions:
        if exc.get("type") == "group" and exc.get("id") in agent_groups:
            return float(exc["value"])

    # 3. Default value
    return float(var.get("value", 0))


def _is_snoozed(rule_id: str, agentid: str, pluginid: str, metric: str) -> bool:
    return is_snoozed(rule_id, agentid, pluginid, metric)



def _get_agent_groups(agentid: str) -> list[str]:
    """Get the groups an agent belongs to from agents.json."""
    try:
        agents_file = os.path.join(CONF_DIR, "agents.json")
        with open(agents_file, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("agents", {}).get(agentid, {}).get("groups", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _load_blackouts() -> list[dict]:
    try:
        with open(BLACKOUTS_FILE, encoding="utf-8") as f:
            return list(json.load(f).values())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _check_blackout(agentid: str, rule_id: str) -> tuple[bool, str | None]:
    """Check if a blackout is active for this agent+rule.
    Returns (should_block, mode) where mode is 'no_alarms' or 'no_notifications'.
    """
    blackouts = _load_blackouts()
    if not blackouts:
        return False, None

    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 6=Sun
    time_str = now.strftime("%H:%M")

    for b in blackouts:
        if not b.get("enabled", True):
            continue

        weekdays = b.get("weekdays", [])
        if weekdays and weekday not in weekdays:
            continue

        start = b.get("start_time", "")
        end = b.get("end_time", "")
        if start and end:
            if end >= start:
                # Same-day window (e.g. 08:00–22:00)
                if not (start <= time_str <= end):
                    continue
            else:
                # Overnight window (e.g. 22:00–06:00): active if time >= start OR time <= end
                if not (time_str >= start or time_str <= end):
                    continue

        target_rules = b.get("target_rules", [])
        target_agents = b.get("target_agents", [])
        target_groups = b.get("target_groups", [])

        if rule_id in target_rules or agentid in target_agents:
            return True, b.get("mode", "no_alarms")

        if target_groups:
            agent_groups = _get_agent_groups(agentid)
            if any(g in target_groups for g in agent_groups):
                return True, b.get("mode", "no_alarms")

        if not target_rules and not target_agents and not target_groups:
            continue

    return False, None


def _maybe_create_alarm(
    session: Session,
    agentid: str,
    rule: Rule,
    metric: str,
    value: float,
    metric_id: int,
    post_commit_actions: list[PostCommitAction] | None = None,
) -> None:
    blocked, mode = _check_blackout(agentid, rule.id)
    if blocked:
        if mode == "no_notifications":
            create_alarm(
                session,
                agentid,
                rule,
                metric,
                value,
                metric_id,
                suppress_notifications=True,
                post_commit_actions=post_commit_actions,
            )
        return
    create_alarm(
        session,
        agentid,
        rule,
        metric,
        value,
        metric_id,
        post_commit_actions=post_commit_actions,
    )


def _ack_open_alarms(session: Session, agentid: str, rule: Rule, metric: str) -> None:
    now = datetime.now(timezone.utc)
    q = (
        select(Alarm)
        .where(
            Alarm.agentid == agentid,
            Alarm.rule_id == rule.id,
            Alarm.pluginid == rule.pluginid,
            Alarm.metric == metric,
            Alarm.acknowledged == False,
        )
    )
    for alarm in session.execute(q).scalars().all():
        alarm.acknowledged = True
        alarm.acknowledged_at = now
        alarm.ack_method = "auto_close"


def create_alarm(
    session: Session,
    agentid: str,
    rule: Rule,
    metric: str,
    value: float,
    metric_id: int,
    suppress_notifications: bool = False,
    post_commit_actions: list[PostCommitAction] | None = None,
) -> None:
    # fire=single: nur einen offenen Alarm pro (agentid, rule)
    if rule.fire == "single" and has_open_alarm(session, agentid, rule):
        return

    # fire=replace: bestehende offene Alarme acknoledgen, dann neuen auslösen
    if rule.fire == "replace":
        _ack_open_alarms(session, agentid, rule, metric)

    # snoozed: skip alarm creation for this combo
    if _is_snoozed(rule.id, agentid, rule.pluginid, metric):
        return

    message = f"Rule '{rule.id}' triggered for agent '{agentid}', plugin '{rule.pluginid}', metric '{metric}': value={value}"
    logger.info("%s", message)
    alarm = Alarm(
        agentid=agentid,
        rule_id=rule.id,
        pluginid=rule.pluginid,
        metric=metric,
        severity=rule.severity,
        value=value,
        message=message,
        metrics_id=metric_id,
    )
    session.add(alarm)
    session.flush()  # ensure alarm.id is available before commit
    alarm_id = alarm.id

    def run_post_commit_actions() -> list[dict[str, str]]:
        # Do not hold the SQLite write transaction during network or shell I/O.
        if not suppress_notifications:
            try:
                notify_targets(rule, agentid, metric, value, message, alarm_id)
            except Exception as e:
                logger.error("Notification failed for rule '%s', agent '%s': %s", rule.id, agentid, e)

        try:
            return run_executors(rule, agentid, metric, value, message)
        except Exception as e:
            logger.error("Executor failed for rule '%s', agent '%s': %s", rule.id, agentid, e)
            return []

    if post_commit_actions is None:
        run_post_commit_actions()
    else:
        post_commit_actions.append(run_post_commit_actions)


def _rule_applies_to_agent(rule: Rule, agentid: str) -> bool:
    agents = rule.agents or []
    if not agents:
        return True
    if rule.agents_mode == "include":
        return agentid in agents
    return agentid not in agents


def evaluate_single_rule(
    session: Session,
    agentid: str,
    pluginid: str,
    metric: str,
    rule: Rule,
    trigger_metric: Metrics,
    post_commit_actions: list[PostCommitAction] | None = None,
) -> None:
    if not _rule_applies_to_agent(rule, agentid):
        return
    base_filter = (
        (Metrics.agentid == agentid),
        (Metrics.pluginid == pluginid),
        (Metrics.metric == metric),
    )

    value = get_value_from_row(trigger_metric)
    if value is None:
        return
    # String metrics (e.g. service status "running"/"stopped") cannot be evaluated
    # with numeric conditions like "gt", "lt" — skip silently
    if isinstance(value, str):
        return

    if rule.scope == "single":
        try:
            v = float(value)
            if compare_rule_value(v, rule, agentid):
                _maybe_create_alarm(
                    session,
                    agentid,
                    rule,
                    metric,
                    v,
                    trigger_metric.id,
                    post_commit_actions,
                )
            elif rule.auto_close:
                _ack_open_alarms(session, agentid, rule, metric)
        except (ValueError, TypeError) as e:
            logger.warning("rule '%s' skipped: cannot convert metric='%s' value=%r to float: %s", rule.id, metric, value, e)

    elif rule.scope == "moving_avg":
        window = rule.window_size or 10
        q = select(func.avg(func.coalesce(Metrics.value_float, Metrics.value_int))).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(window)
        avg_value = session.execute(q).scalar()
        if avg_value is None:
            return
        try:
            v = float(avg_value)
            if compare_rule_value(v, rule, agentid):
                _maybe_create_alarm(
                    session,
                    agentid,
                    rule,
                    metric,
                    v,
                    trigger_metric.id,
                    post_commit_actions,
                )
            elif rule.auto_close:
                _ack_open_alarms(session, agentid, rule, metric)
        except (ValueError, TypeError) as e:
            logger.warning("rule '%s' moving_avg: cannot convert avg=%r to float: %s", rule.id, avg_value, e)

    elif rule.scope == "count_ratio":
        window = rule.window_size or 10
        min_violations = rule.min_violations or 1
        q = select(func.coalesce(Metrics.value_float, Metrics.value_int).label("v")).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(window)
        values = [row.v for row in session.execute(q) if row.v is not None]
        if not values:
            return
        try:
            violations = sum(1 for v in values if compare_rule_value(float(v), rule, agentid))
        except (ValueError, TypeError) as e:
            logger.warning("rule '%s' count_ratio: cannot convert value to float: %s", rule.id, e)
            return
        if violations >= min_violations:
            _maybe_create_alarm(
                session,
                agentid,
                rule,
                metric,
                float(value),
                trigger_metric.id,
                post_commit_actions,
            )
        elif rule.auto_close:
            _ack_open_alarms(session, agentid, rule, metric)

    elif rule.scope == "change":
        previous = session.query(
            func.coalesce(Metrics.value_float, Metrics.value_int)
        ).where(
            *base_filter,
            Metrics.id < trigger_metric.id,
        ).order_by(desc(Metrics.timestamp)).limit(1).scalar()

        if previous is None:
            return
        try:
            v = float(value)
            prev = float(previous)
            delta = v - prev
            if compare_rule_value(delta, rule, agentid):
                _maybe_create_alarm(
                    session,
                    agentid,
                    rule,
                    metric,
                    delta,
                    trigger_metric.id,
                    post_commit_actions,
                )
            elif rule.auto_close:
                _ack_open_alarms(session, agentid, rule, metric)
        except (ValueError, TypeError) as e:
            logger.warning("rule '%s' change: cannot compute delta: %s", rule.id, e)


def evaluate_rules_for_payload(
    session: Session,
    agentid: str,
    pluginid: str,
    saved_metrics: list[Metrics],
) -> list[PostCommitAction]:
    post_commit_actions: list[PostCommitAction] = []
    relevant_rules = [
        r for r in load_rules()
        if r.enabled and r.pluginid == pluginid and r.condition != "no_data"
    ]
    if not relevant_rules:
        return post_commit_actions

    for metric_obj in saved_metrics:
        for rule in relevant_rules:
            if not metric_matches(rule.metric, metric_obj.metric):
                continue
            evaluate_single_rule(
                session,
                agentid,
                pluginid,
                metric_obj.metric,
                rule,
                metric_obj,
                post_commit_actions,
            )

    return post_commit_actions
