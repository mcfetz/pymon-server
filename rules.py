import json
import os
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from db_models import Alarm, Metrics
from functions import get_value_from_row
from notifications import notify_targets
from cache import timed_cache
from executors import run_executors

Condition = Literal["gt", "lt", "ge", "le", "eq", "ne"]
Scope = Literal["single", "moving_avg", "count_ratio"]
FireMode = Literal["single", "multi", "replace"]


@dataclass
class Rule:
    id: str
    enabled: bool
    description: str
    pluginid: str
    metric: str
    condition: Condition
    threshold: float
    scope: Scope
    window_size: int | None = None
    min_violations: int | None = None
    severity: str = "warning"
    notifications: list[str] | None = None
    fire: FireMode = "single"
    executors: list[str] | None = None


@timed_cache(ttl_seconds=5)
def load_rules(path: str = "conf/rules.json") -> list[Rule]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    rules: list[Rule] = []
    for rule_id, r in raw.items():
        rules.append(
            Rule(
                id=rule_id,
                enabled=r.get("enabled", True),
                description=r.get("description", ""),
                pluginid=r.get("pluginid", ""),
                metric=r.get("metric", ""),
                condition=r.get("condition", "gt"),
                threshold=float(r.get("threshold", 0)),
                scope=r.get("scope", "single"),
                window_size=r.get("window_size"),
                min_violations=r.get("min_violations"),
                severity=r.get("severity", "warning"),
                notifications=r.get("notifications", []),
                fire=r.get("fire", "single"),
                executors=r.get("executors", []),
            )
        )
    return rules


def compare(value: float, condition: Condition, threshold: float) -> bool:
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


SNOOZE_FILE = os.path.join(os.path.dirname(__file__), "conf", "snoozes.json")


def _is_snoozed(rule_id: str, agentid: str, pluginid: str, metric: str) -> bool:
    try:
        with open(SNOOZE_FILE, encoding="utf-8") as f:
            snoozes = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    for s in snoozes:
        if s.get("rule_id") == rule_id and s.get("agentid") == agentid and s.get("pluginid") == pluginid and s.get("metric") == metric:
            return True
    return False


def _ack_open_alarms(session: Session, agentid: str, rule: Rule, metric: str) -> None:
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


def create_alarm(
    session: Session,
    agentid: str,
    rule: Rule,
    metric: str,
    value: float,
    metric_id: int,
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
    print(message)
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

    # Notifications auslösen (Fehler hier sollen die DB-Transaktion nicht verhindern)
    try:
        notify_targets(rule, agentid, metric, value, message, alarm.id)
    except Exception:
        pass

    # Executor ausführen (Fehler hier sollen die DB-Transaktion ebenfalls nicht verhindern)
    try:
        run_executors(rule, agentid, metric, value, message)
    except Exception:
        pass


def evaluate_single_rule(
    session: Session,
    agentid: str,
    pluginid: str,
    metric: str,
    rule: Rule,
    trigger_metric: Metrics,
) -> None:
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
        if compare(float(value), rule.condition, rule.threshold):
            create_alarm(session, agentid, rule, metric, float(value), trigger_metric.id)

    elif rule.scope == "moving_avg":
        window = rule.window_size or 10
        q = select(func.avg(func.coalesce(Metrics.value_float, Metrics.value_int))).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(window)
        avg_value = session.execute(q).scalar()
        if avg_value is None:
            return
        if compare(float(avg_value), rule.condition, rule.threshold):
            create_alarm(session, agentid, rule, metric, float(avg_value), trigger_metric.id)

    elif rule.scope == "count_ratio":
        window = rule.window_size or 10
        min_violations = rule.min_violations or 1
        q = select(func.coalesce(Metrics.value_float, Metrics.value_int).label("v")).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(window)
        values = [row.v for row in session.execute(q) if row.v is not None]
        if not values:
            return
        violations = sum(1 for v in values if compare(float(v), rule.condition, rule.threshold))
        if violations >= min_violations:
            create_alarm(session, agentid, rule, metric, float(violations), trigger_metric.id)


def evaluate_rules_for_payload(
    session: Session,
    agentid: str,
    pluginid: str,
    saved_metrics: list[Metrics],
) -> None:
    relevant_rules = [r for r in load_rules() if r.enabled and r.pluginid == pluginid]
    if not relevant_rules:
        return

    for metric_obj in saved_metrics:
        for rule in relevant_rules:
            if rule.metric not in (metric_obj.metric, "*"):
                continue
            evaluate_single_rule(session, agentid, pluginid, metric_obj.metric, rule, metric_obj)
