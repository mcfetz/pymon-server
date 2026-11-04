import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import jwt
from flask import request, jsonify

from core import app, logger
from config import CONF_DIR
from users import (
    UserError,
    UserExists,
    UserNotFound,
    get_user,
    public_user,
    update_account as update_user_account,
    verify_credentials,
)
from audit import record_audit

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
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "invalid token"}), 401

    username = payload.get("sub")
    user = get_user(username)
    if not user:
        return jsonify({"error": "invalid token"}), 401
    try:
        token_version = int(payload.get("ver", 0))
    except (TypeError, ValueError):
        token_version = 0
    if token_version != int(user.get("token_version") or 0):
        return jsonify({"error": "session expired"}), 401

    request.frontend_user = username
    request.frontend_role = user.get("role", "user")
    request.frontend_display_name = user.get("display_name") or username


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

    user = get_user(username)
    if not user or not verify_credentials(username, password):
        return jsonify({"error": "invalid credentials"}), 401

    payload = {
        "sub": username,
        "role": user.get("role", "user"),
        "ver": int(user.get("token_version") or 0),
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    token = jwt.encode(payload, _load_secret(), algorithm=JWT_ALGO)
    return jsonify(
        {
            "token": token,
            "username": username,
            "role": user.get("role", "user"),
            "display_name": user.get("display_name") or username,
        }
    ), 200


@app.route("/account", methods=["GET"])
def get_account():
    username = getattr(request, "frontend_user", None)
    if not username:
        return jsonify({"error": "unauthorized"}), 401
    user = get_user(username)
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(public_user(username, user)), 200


@app.route("/account", methods=["PUT"])
def update_account():
    username = getattr(request, "frontend_user", None)
    if not username:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password", "")
    new_username = str(data.get("new_username") or "").strip()
    new_password = data.get("new_password") or ""
    display_name = data.get("display_name")

    if not verify_credentials(username, current_pw):
        return jsonify({"error": "current password is wrong"}), 403
    if not new_username and not new_password and display_name is None:
        return jsonify({"error": "nothing to update"}), 400

    changed = []
    if new_username and new_username != username:
        changed.append("username")
    if new_password:
        changed.append("password")
    if display_name is not None:
        changed.append("display_name")

    try:
        target, user = update_user_account(
            username,
            new_username=new_username or None,
            new_password=new_password or None,
            display_name=display_name,
        )
    except UserExists:
        return jsonify({"error": "username already taken"}), 409
    except UserNotFound:
        return jsonify({"error": "unauthorized"}), 401
    except UserError as exc:
        return jsonify({"error": str(exc)}), 400

    record_audit(
        "user.account_update",
        "user",
        target,
        summary=f"updated own account ({', '.join(changed)})",
        details={"changed_fields": changed},
    )

    payload = {
        "sub": target,
        "role": user.get("role", "user"),
        "ver": int(user.get("token_version") or 0),
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_TTL_DAYS),
    }
    token = jwt.encode(payload, _load_secret(), algorithm=JWT_ALGO)
    return jsonify(
        {
            "token": token,
            "username": target,
            "role": user.get("role", "user"),
            "display_name": user.get("display_name") or target,
        }
    ), 200