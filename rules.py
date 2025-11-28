from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Literal, Any
import smtplib
from email.message import EmailMessage

import toml
from sqlalchemy import select, desc, func
from sqlalchemy.orm import Session

from db_models import Metrics, Alarm

Condition = Literal["gt", "lt", "ge", "le", "eq", "ne"]
Scope = Literal["single", "moving_avg", "count_ratio"]


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


def load_rules(path: str = "rules.toml") -> list[Rule]:
    data = toml.load(path)
    rules: list[Rule] = []
    for r in data.get("rule", []):
        rules.append(
            Rule(
                id=r["id"],
                enabled=r.get("enabled", True),
                description=r.get("description", ""),
                pluginid=r["pluginid"],
                metric=r["metric"],
                condition=r["condition"],
                threshold=float(r["threshold"]),
                scope=r.get("scope", "single"),
                window_size=r.get("window_size"),
                min_violations=r.get("min_violations"),
                severity=r.get("severity", "warning"),
                notifications=r.get("notifications", []),
            )
        )
    return rules


RULES: list[Rule] = load_rules()


def load_notification_config(path: str = "notifications.toml") -> dict[str, Any]:
    try:
        data = toml.load(path)
    except FileNotFoundError:
        return {}
    return data.get("targets", {})


NOTIFICATION_TARGETS: dict[str, Any] = load_notification_config()


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
    raise ValueError(f"Unknown condition: {condition}")


def send_email_notification(target_conf: dict[str, Any], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = target_conf["from"]
    msg["To"] = target_conf["to"]
    msg.set_content(body)

    server = target_conf["server"]
    port = int(target_conf.get("port", 587))
    user = target_conf.get("user")
    password = target_conf.get("password")
    use_tls = bool(target_conf.get("use_tls", True))

    if use_tls:
        with smtplib.SMTP(server, port) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            print(f"send msg: {msg}")
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(server, port) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)


def notify_targets(rule: Rule, agentid: str, metric: str, value: float, message: str) -> None:
    if not rule.notifications:
        return

    for target_name in rule.notifications:
        target_conf = NOTIFICATION_TARGETS.get(target_name)
        if not target_conf:
            continue

        target_type = target_conf.get("type")
        if target_type == "email":
            subject = f"[skript] Alarm {rule.severity}: {rule.id} on {agentid}"
            body = (
                "Alarm triggered\n\n"
                f"Rule: {rule.id}\n"
                f"Agent: {agentid}\n"
                f"Plugin: {rule.pluginid}\n"
                f"Metric: {metric}\n"
                f"Value: {value}\n"
                f"Severity: {rule.severity}\n\n"
                f"Message: {message}\n"
            )
            send_email_notification(target_conf, subject, body)
        # weitere Typen (webhook, slack, ...) können hier später ergänzt werden


def create_alarm(
    session: Session,
    agentid: str,
    rule: Rule,
    metric: str,
    value: float,
) -> None:
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
    )
    session.add(alarm)

    # Notifications auslösen (Fehler hier sollen die DB-Transaktion nicht verhindern)
    try:
        notify_targets(rule, agentid, metric, value, message)
    except Exception:
        # Optional: Logging kann hier ergänzt werden
        pass


def evaluate_single_rule(
    session: Session,
    agentid: str,
    pluginid: str,
    metric: str,
    rule: Rule,
) -> None:
    base_filter = (
        (Metrics.agentid == agentid),
        (Metrics.pluginid == pluginid),
        (Metrics.metric == metric),
    )

    if rule.scope == "single":
        q = select(Metrics).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(1)
        row = session.execute(q).scalars().first()
        if not row:
            return
        value = row.value_float if row.value_float is not None else row.value_int
        if value is None:
            return
        if compare(float(value), rule.condition, rule.threshold):
            create_alarm(session, agentid, rule, metric, float(value))

    elif rule.scope == "moving_avg":
        window = rule.window_size or 10
        q = select(func.avg(func.coalesce(Metrics.value_float, Metrics.value_int))).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(window)
        avg_value = session.execute(q).scalar()
        if avg_value is None:
            return
        if compare(float(avg_value), rule.condition, rule.threshold):
            create_alarm(session, agentid, rule, metric, float(avg_value))

    elif rule.scope == "count_ratio":
        window = rule.window_size or 10
        min_violations = rule.min_violations or 1
        q = select(func.coalesce(Metrics.value_float, Metrics.value_int).label("v")).where(*base_filter).order_by(desc(Metrics.timestamp)).limit(window)
        values = [row.v for row in session.execute(q) if row.v is not None]
        if not values:
            return
        violations = sum(1 for v in values if compare(float(v), rule.condition, rule.threshold))
        if violations >= min_violations:
            create_alarm(session, agentid, rule, metric, float(violations))


def evaluate_rules_for_payload(
    session: Session,
    agentid: str,
    pluginid: str,
    metrics_list: list[dict],
) -> None:
    relevant_rules = [r for r in RULES if r.enabled and r.pluginid == pluginid]
    if not relevant_rules:
        return

    for metric_dict in metrics_list:
        for metric_name in metric_dict.keys():
            for rule in relevant_rules:
                if rule.metric != metric_name and rule.metric != "*":
                    continue
                evaluate_single_rule(session, agentid, pluginid, metric_name, rule)
