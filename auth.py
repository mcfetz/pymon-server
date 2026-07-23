import hmac
import json
import os
from functools import wraps

from flask import request, jsonify

from core import logger


def verify_agent_apikey(agentid: str, apikey: str) -> bool:
    if not agentid or not apikey:
        return False

    try:
        agents_json = os.path.join(os.path.dirname(__file__), "conf", "agents.json")
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
