import toml
from functools import wraps

from flask import request, jsonify

from core import logger


def verify_agent_apikey(agentid: str, apikey: str) -> bool:
    """
    Verify that the given API key is valid for the given agentid.

    Returns True if a matching entry exists in conf/apikeys.toml with:
      - type = "agent"
      - table name == agentid
      - key == apikey
    """
    if not agentid or not apikey:
        return False

    try:
        config = toml.load("conf/apikeys.toml")
    except Exception as e:
        logger.error("Error while loading apikeys.toml: %s", e)
        return False

    entry = config.get(agentid)
    if not entry:
        return False

    if entry.get("type") != "agent":
        return False

    stored_key = entry.get("key")
    if not stored_key:
        return False

    return stored_key == apikey


def require_agent_apikey(func):
    """
    Decorator to enforce agentid + X-API-Key authentication on a route.

    Expects headers:
      - agentid: the agent identifier
      - X-API-Key: the API key for that agent

    On success:
      - calls the wrapped function
      - sets request.agentid for convenience
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        agentid = request.headers.get("agentid", None)
        apikey = request.headers.get("X-API-Key", None)

        if not agentid:
            return jsonify({"error": "agentid header missing"}), 400

        if not apikey or not verify_agent_apikey(agentid, apikey):
            return jsonify({"error": "invalid or missing API key"}), 401

        # Attach agentid to request for use in the view
        request.agentid = agentid
        return func(*args, **kwargs)

    return wrapper
