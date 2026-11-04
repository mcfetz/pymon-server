import hmac
import json
import os
from functools import wraps

from flask import request, jsonify

from core import logger
from config import CONF_DIR


def verify_agent_apikey(agentid: str, apikey: str) -> bool:
    if not agentid or not apikey:
        return False

    try:
        agents_json = os.path.join(CONF_DIR, "agents.json")
        if os.path.exists(agents_json):
            with open(agents_json, encoding="utf-8") as f:
                cfg = json.load(f)
            agent = cfg.get("agents", {}).get(agentid)
            if agent and agent.get("enabled", True) is not False:
                stored = agent.get("apikey", "")
                if stored and hmac.compare_digest(stored, apikey):
                    return True
    except Exception as e:
        logger.error("Error reading agents.json during API key verification: %s", e)

    return False


def require_agent_apikey(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if getattr(request, 'frontend_user', None):
            return func(*args, **kwargs)

        agentid = request.headers.get("agentid", None)
        apikey = request.headers.get("X-API-Key", None)

        if not agentid:
            return jsonify({"error": "agentid header missing"}), 400

        if not apikey or not verify_agent_apikey(agentid, apikey):
            return jsonify({"error": "invalid or missing API key"}), 401

        request.agentid = agentid
        return func(*args, **kwargs)

    return wrapper


def require_admin(func):
    """Allow only a logged-in frontend user with role=admin."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if getattr(request, "frontend_role", None) != "admin":
            return jsonify({"error": "admin required"}), 403
        return func(*args, **kwargs)

    return wrapper


def require_user(func):
    """Allow frontend users (admin/user) and authenticated agents."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        role = getattr(request, "frontend_role", None)
        if role in ("admin", "user") or getattr(request, "agentid", None):
            return func(*args, **kwargs)
        return jsonify({"error": "user required"}), 403

    return wrapper


def require_frontend_user(func):
    """Allow frontend users (admin/user) but not agent API keys."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if getattr(request, "frontend_role", None) in ("admin", "user"):
            return func(*args, **kwargs)
        return jsonify({"error": "user required"}), 403

    return wrapper
