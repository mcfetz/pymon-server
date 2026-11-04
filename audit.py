"""Append-only audit trail for alarm, configuration and user changes."""

import json
import logging

from flask import has_request_context, request

from core import SessionLocal
from db_models import AuditLog

logger = logging.getLogger(__name__)

_REDACT_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "authorization",
    "webhook",
    "private_key",
)


def _redact(value):
    """Remove credential-bearing values from audit details."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _REDACT_MARKERS):
                result[key] = "***"
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def current_actor() -> tuple[str, str]:
    if has_request_context():
        username = getattr(request, "frontend_user", None)
        if username:
            return "user", username
        agentid = getattr(request, "agentid", None)
        if agentid:
            return "agent", agentid
    return "system", "system"


def record_audit(
    action: str,
    target_type: str,
    target_id=None,
    summary: str = "",
    details=None,
    session=None,
    actor: tuple[str, str] | None = None,
):
    """Add one audit entry.

    ``session`` is optional: pass the transaction's session for atomic alarm
    changes, or omit it for file-backed configuration writes.
    """
    actor_type, actor_id = actor or current_actor()
    ip = None
    user_agent = None
    if has_request_context():
        ip = request.remote_addr
        user_agent = (request.headers.get("User-Agent") or "")[:255] or None

    try:
        details_text = json.dumps(
            _redact(details or {}), ensure_ascii=False, default=str
        )
        if len(details_text) > 8000:
            details_text = json.dumps({"truncated": True})
    except (TypeError, ValueError):
        details_text = "{}"

    entry = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        summary=(summary or "")[:1000],
        details=details_text,
        ip=ip,
        user_agent=user_agent,
    )
    if session is not None:
        session.add(entry)
        return entry

    try:
        with SessionLocal() as db:
            db.add(entry)
            db.commit()
    except Exception as exc:  # audit must not break a successful config write
        logger.error("Could not write audit entry %s %s: %s", action, target_id, exc)
    return entry


def audit_config(
    action: str,
    target_type: str,
    target_id,
    before=None,
    after=None,
    summary: str = "",
):
    """Record a configuration change using field names, never secret values."""
    details = {}
    if before is None and after is not None:
        details["created"] = True
    elif before is not None and after is None:
        details["deleted"] = True
    elif isinstance(before, dict) and isinstance(after, dict):
        keys = set(before) | set(after)
        details["changed_fields"] = sorted(
            str(key) for key in keys if before.get(key) != after.get(key)
        )
    record_audit(
        action,
        target_type,
        target_id,
        summary=summary or f"{action} {target_type} {target_id}",
        details=details,
    )
