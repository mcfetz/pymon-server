import json
import os
import time
import threading
from collections import defaultdict
from functools import wraps
from datetime import datetime, timedelta, timezone

import jwt
from flask import request, jsonify
from werkzeug.security import check_password_hash, generate_password_hash

from core import app, logger

CONF_DIR = os.path.join(os.path.dirname(__file__), "..", "conf")
USERS_FILE = os.path.join(CONF_DIR, "users.json")
SECRET_FILE = os.path.join(CONF_DIR, "jwt_secret.txt")

JWT_ALGO = "HS256"
JWT_TTL_DAYS = 30

# Routes that agent uses directly (no frontend auth needed)
AGENT_ROUTE_PREFIXES = ("/push/", "/agent/", "/plugins")

# ── Rate limiting for /login ──
_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_lock = threading.Lock()
_RATE_WINDOW = 60   # seconds
_RATE_MAX = 10      # max attempts per window


def _check_rate_limit(ip: str) -> bool:
    """Return True if this IP has exceeded the login rate limit."""
    now = time.time()
    with _login_lock:
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _RATE_WINDOW]
        if len(_login_attempts[ip]) >= _RATE_MAX:
            return True
        _login_attempts[ip].append(now)
        return False


def _load_secret() -> str:
    if not os.path.exists(SECRET_FILE):
        import secrets as _secrets
        secret = _secrets.token_hex(32)
        os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
        with open(SECRET_FILE, "w") as f:
            f.write(secret)
        logger.info("Generated new JWT secret at %s", SECRET_FILE)
        return secret
    try:
        with open(SECRET_FILE) as f:
            return f.read().strip()
    except OSError as e:
        logger.critical("Cannot read JWT secret from %s: %s", SECRET_FILE, e)
        raise RuntimeError(f"Cannot read JWT secret: {e}") from e


def _load_users() -> dict[str, str]:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE) as f:
        return json.load(f)


@app.before_request
def check_frontend_auth():
    if request.path == "/login":
        return
    for prefix in AGENT_ROUTE_PREFIXES:
        if request.path.startswith(prefix):
            return
    if request.headers.get("agentid"):
        from auth import verify_agent_apikey
        agentid = request.headers.get("agentid", "")
        apikey = request.headers.get("X-API-Key", "")
        if verify_agent_apikey(agentid, apikey):
            return
        return jsonify({"error": "invalid agent credentials"}), 401
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401
    token = auth[7:]
    try:
        payload = jwt.decode(token, _load_secret(), algorithms=[JWT_ALGO])
        request.frontend_user = payload.get("sub")
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "invalid token"}), 401


@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr or "unknown"
    if _check_rate_limit(ip):
        logger.warning("Login rate limit exceeded for IP %s", ip)
        return jsonify({"error": "too many requests, try again later"}), 429

    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    users = _load_users()
    pw_hash = users.get(username)
    if not pw_hash or not check_password_hash(pw_hash, password):
        return jsonify({"error": "invalid credentials"}), 401

    payload = {
        "sub": username,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    token = jwt.encode(payload, _load_secret(), algorithm=JWT_ALGO)
    return jsonify({"token": token, "username": username}), 200


@app.route("/account", methods=["PUT"])
def update_account():
    user = getattr(request, "frontend_user", None)
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password", "")
    new_username = data.get("new_username", "").strip()
    new_password = data.get("new_password", "")

    users = _load_users()
    pw_hash = users.get(user)
    if not pw_hash or not check_password_hash(pw_hash, current_pw):
        return jsonify({"error": "current password is wrong"}), 403

    if not new_username and not new_password:
        return jsonify({"error": "nothing to update"}), 400

    if new_username and new_username != user and new_username in users:
        return jsonify({"error": "username already taken"}), 409

    if new_username:
        users[new_username] = users.pop(user)
        user = new_username

    if new_password:
        users[user] = generate_password_hash(new_password)

    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

    payload = {
        "sub": user,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    token = jwt.encode(payload, _load_secret(), algorithm=JWT_ALGO)
    return jsonify({"token": token, "username": user}), 200