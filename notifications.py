import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Any

import rules
from core import logger
from services.web_push import send_push_notification


def _load_notifications_json() -> dict[str, Any]:
    fpath = os.path.join(os.path.dirname(__file__), "conf", "notifications.json")
    try:
        with open(fpath, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


NOTIFICATION_TARGETS: dict[str, Any] = _load_notifications_json()


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


def _is_notify_enabled(target_name: str) -> bool:
    import json, os
    notify_json = os.path.join(os.path.dirname(__file__), "conf", "notifications.json")
    try:
        with open(notify_json, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get(target_name, {}).get("enabled", True)
    except Exception:
        return True


def _get_notify_config(target_name: str) -> dict:
    """Read notification config from JSON (source of truth)."""
    notify_json = os.path.join(os.path.dirname(__file__), "conf", "notifications.json")
    try:
        with open(notify_json, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get(target_name, {})
    except Exception:
        return {}


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
        if not _is_notify_enabled(target_name):
            continue
        cfg = _get_notify_config(target_name)
        if not cfg:
            continue

        target_type = cfg.get("type")
        if target_type == "email":
            target_conf = NOTIFICATION_TARGETS.get(target_name)
            if not target_conf:
                continue
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

        elif target_type == "web_push":
            push_title = f"[{rule.severity}] {rule.id}"
            push_body = f"Agent: {agentid} | {rule.pluginid}/{metric} = {value}"
            send_push_notification(
                push_title,
                push_body,
                tag=f"pymon-{rule.id}",
                private_key=cfg.get("vapid_private_key") or None,
                public_key=cfg.get("vapid_public_key") or None,
                subject=cfg.get("vapid_subject") or None,
            )

        elif target_type == "ntfy":
            ntfy_url = (cfg.get("ntfy_url") or "https://ntfy.sh").rstrip("/")
            topic = cfg.get("ntfy_topic")
            if not topic:
                continue
            token = cfg.get("ntfy_access_token") or None
            ntfy_body = json.dumps({
                "topic": topic,
                "title": f"[{rule.severity}] {rule.id}",
                "message": (f"Agent: {agentid} | {rule.pluginid}/{metric} = {value}\n"
                            f"Rule: {rule.id}\n{message}"),
                "tags": [rule.severity],
                "priority": 4 if rule.severity == "critical" else 3,
            }).encode()
            req = urllib.request.Request(
                ntfy_url,
                data=ntfy_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                logger.error("ntfy notification failed: %s", exc)

        elif target_type == "twilio_call":
            sid = cfg.get("twilio_account_sid")
            token = cfg.get("twilio_auth_token")
            call_from = cfg.get("twilio_call_from")
            call_to = cfg.get("twilio_call_to")
            if not all([sid, token, call_from, call_to]):
                continue
            try:
                from twilio.rest import Client
                client = Client(sid, token)
                msg = f"Alert from pymon. Rule {rule.id} triggered for agent {agentid}. {metric} is {value}."
                client.calls.create(
                    twiml=f"<Response><Say>{msg}</Say></Response>",
                    to=call_to,
                    from_=call_from,
                )
            except Exception as exc:
                logger.error("twilio call notification failed: %s", exc)
