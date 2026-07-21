import os
import smtplib
from email.message import EmailMessage
from typing import Any

import toml

import rules
from core import logger


def load_notification_config(path: str = "conf/notifications.toml") -> dict[str, Any]:
    try:
        data = toml.load(path)
    except FileNotFoundError:
        return {}
    return data.get("targets", {})


NOTIFICATION_TARGETS: dict[str, Any] = load_notification_config()


def send_email_notification(target_conf: dict[str, Any], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = target_conf["from"]
    msg["To"] = target_conf["to"]
    msg.set_content(body)

    server = target_conf["server"]
    port = int(target_conf.get("port", 587))
    user = target_conf.get("user")
    password = target_conf.get("password") or os.environ.get("NOTIFY_EMAIL_PASSWORD")
    use_tls = bool(target_conf.get("use_tls", True))

    if use_tls:
        with smtplib.SMTP(server, port) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            logger.info("Sending email notification to %s", target_conf.get("to"))
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(server, port) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)


def notify_targets(
    rule: rules.Rule,
    agentid: str,
    metric: str,
    value: float,
    message: str,
    alarm_id: int | None = None,
) -> None:
    if not rule.notifications:
        return

    for target_name in rule.notifications:
        target_conf = NOTIFICATION_TARGETS.get(target_name)
        if not target_conf:
            continue

        target_type = target_conf.get("type")
        if target_type == "email":
            subject = f"[pymon] Alarm {rule.severity}: {rule.id} on {agentid}"
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

            if alarm_id is not None:
                ack_link = f"http://localhost:5000/alarms/{alarm_id}/ack"
                body += f"\nAcknowledge this alarm: {ack_link}\n"

            send_email_notification(target_conf, subject, body)
        # additional types (webhook, slack, ...) can be added here later
