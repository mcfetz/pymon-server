import json
import os
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


def _load_secret() -> str:
    with open(SECRET_FILE) as f:
        return f.read().strip()


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