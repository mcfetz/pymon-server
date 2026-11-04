"""Read-only audit trail API."""

import json
from datetime import UTC, datetime

from flask import jsonify, request
from sqlalchemy import desc

from auth import require_agent_apikey, require_frontend_user
from core import app, SessionLocal
from db_models import AuditLog
from users import get_user, load_users


def _parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _entry_to_dict(entry: AuditLog, users: dict | None = None) -> dict:
    details = entry.details
    if details:
        try:
            details = json.loads(details)
        except (TypeError, ValueError):
            pass
    actor_label = entry.actor_id
    if entry.actor_type == "user" and entry.actor_id:
        user = (users or {}).get(entry.actor_id) or get_user(entry.actor_id)
        if user:
            actor_label = user.get("display_name") or entry.actor_id
    return {
        "id": entry.id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "actor_type": entry.actor_type,
        "actor_id": entry.actor_id,
        "actor_label": actor_label,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "summary": entry.summary,
        "details": details,
        "ip": entry.ip,
    }


@app.route("/admin/audit", methods=["GET"])
@require_agent_apikey
@require_frontend_user
def list_audit():
    session = SessionLocal()
    try:
        query = session.query(AuditLog)
        actor_id = (request.args.get("actor_id") or "").strip()
        if actor_id:
            query = query.filter(AuditLog.actor_id == actor_id)
        actor_type = (request.args.get("actor_type") or "").strip()
        if actor_type:
            query = query.filter(AuditLog.actor_type == actor_type)
        action = (request.args.get("action") or "").strip()
        if action:
            query = query.filter(AuditLog.action == action)
        target_type = (request.args.get("target_type") or "").strip()
        if target_type:
            query = query.filter(AuditLog.target_type == target_type)
        target_id = (request.args.get("target_id") or "").strip()
        if target_id:
            query = query.filter(AuditLog.target_id == target_id)
        since = _parse_dt(request.args.get("since"))
        if since:
            query = query.filter(AuditLog.created_at >= since)
        until = _parse_dt(request.args.get("until"))
        if until:
            query = query.filter(AuditLog.created_at <= until)

        limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
        offset = max(request.args.get("offset", 0, type=int), 0)
        rows = (
            query.order_by(desc(AuditLog.created_at), desc(AuditLog.id))
            .offset(offset)
            .limit(limit)
            .all()
        )
        users = load_users()
        return jsonify([_entry_to_dict(row, users) for row in rows]), 200
    finally:
        session.close()
