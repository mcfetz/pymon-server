"""Web Push subscription endpoints."""

import logging
import os

from flask import jsonify, request
from sqlalchemy import desc

from auth import require_agent_apikey
from core import SessionLocal, app, logger
from db_models import Alarm, PushSubscription

logger = logging.getLogger(__name__)

VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")


@app.route("/vapid-public-key", methods=["GET"])
def vapid_public_key():
    """Return the VAPID public key for push subscription."""
    if not VAPID_PUBLIC_KEY:
        return jsonify({"error": "VAPID not configured"}), 500
    return jsonify({"public_key": VAPID_PUBLIC_KEY}), 200


@app.route("/subscribe", methods=["POST"])
@require_agent_apikey
def subscribe_push():
    """Save a push subscription."""
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    p256dh = data.get("p256dh")
    auth = data.get("auth")

    if not endpoint or not p256dh or not auth:
        return jsonify({"error": "endpoint, p256dh, and auth are required"}), 400

    session = SessionLocal()
    try:
        existing = session.query(PushSubscription).filter_by(endpoint=endpoint).first()
        if existing:
            session.delete(existing)
            session.flush()

        sub = PushSubscription(endpoint=endpoint, p256dh_key=p256dh, auth_key=auth)
        session.add(sub)
        session.commit()
        return jsonify({"status": "subscribed"}), 201
    except Exception as e:
        session.rollback()
        logger.error("Error saving subscription: %s", e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()


@app.route("/unsubscribe", methods=["POST"])
@require_agent_apikey
def unsubscribe_push():
    """Remove a push subscription."""
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")

    session = SessionLocal()
    try:
        sub = None
        if endpoint:
            sub = session.query(PushSubscription).filter_by(endpoint=endpoint).first()
        if sub:
            session.delete(sub)
            session.commit()
            return jsonify({"status": "unsubscribed"}), 200
        return jsonify({"error": "not found"}), 404
    except Exception as e:
        session.rollback()
        logger.error("Error removing subscription: %s", e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()