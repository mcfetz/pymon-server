"""User store and password handling for pymon.

The on-disk format used to be ``username -> password-hash``.  It is now
``username -> {password, role, display_name, token_version, created_at}``.
Legacy string entries are normalised in memory and rewritten on the next
mutation, so existing installations keep working without a manual step.
"""

import json
import os
import re
import threading
from datetime import UTC, datetime

from werkzeug.security import check_password_hash, generate_password_hash

from config import CONF_DIR

USERS_FILE = os.path.join(CONF_DIR, "users.json")
ROLES = {"admin", "user"}
DEFAULT_ROLE = "user"
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

_lock = threading.RLock()


class UserError(Exception):
    """Base class for user-store validation errors."""


class UserNotFound(UserError):
    pass


class UserExists(UserError):
    pass


class LastAdminError(UserError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalise(username: str, value) -> dict:
    if isinstance(value, str):
        # Pre-roles format: every existing account was effectively an admin.
        return {
            "password": value,
            "role": "admin",
            "display_name": username,
            "token_version": 0,
            "created_at": None,
        }
    if not isinstance(value, dict):
        return {
            "password": "",
            "role": DEFAULT_ROLE,
            "display_name": username,
            "token_version": 0,
            "created_at": None,
        }
    role = value.get("role") if value.get("role") in ROLES else DEFAULT_ROLE
    try:
        token_version = int(value.get("token_version") or 0)
    except (TypeError, ValueError):
        token_version = 0
    return {
        "password": str(value.get("password") or ""),
        "role": role,
        "display_name": str(value.get("display_name") or username),
        "token_version": token_version,
        "created_at": value.get("created_at") or None,
    }


def _read_raw() -> dict:
    try:
        with open(USERS_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
        return raw if isinstance(raw, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_users() -> dict[str, dict]:
    raw = _read_raw()
    return {str(name): _normalise(str(name), value) for name, value in raw.items()}


def save_users(users: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(USERS_FILE)), exist_ok=True)
    temporary = f"{USERS_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(users, handle, indent=2)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, USERS_FILE)


def get_user(username: str) -> dict | None:
    if not username:
        return None
    with _lock:
        return load_users().get(username)


def verify_credentials(username: str, password: str) -> bool:
    user = get_user(username)
    if not user or not password:
        return False
    try:
        return check_password_hash(user["password"], password)
    except (TypeError, ValueError):
        return False


def public_user(username: str, user: dict) -> dict:
    return {
        "username": username,
        "role": user.get("role", DEFAULT_ROLE),
        "display_name": user.get("display_name") or username,
        "created_at": user.get("created_at"),
    }


def list_public_users() -> list[dict]:
    users = load_users()
    return [public_user(name, user) for name, user in sorted(users.items())]


def _validate_username(username: str) -> str:
    username = str(username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise UserError(
            "username must be 1-32 characters (letters, digits, dot, dash, underscore)"
        )
    return username


def _validate_password(password: str) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def _validate_role(role: str) -> str:
    role = str(role or DEFAULT_ROLE).strip().lower()
    if role not in ROLES:
        raise UserError("role must be one of: admin, user")
    return role


def _admin_count(users: dict[str, dict]) -> int:
    return sum(1 for user in users.values() if user.get("role") == "admin")


def create_user(
    username: str,
    password: str,
    role: str = DEFAULT_ROLE,
    display_name: str = "",
) -> dict:
    username = _validate_username(username)
    password = _validate_password(password)
    role = _validate_role(role)
    with _lock:
        users = load_users()
        if username in users:
            raise UserExists("user already exists")
        display_name = str(display_name or username)
        user = {
            "password": generate_password_hash(password),
            "role": role,
            "display_name": display_name.strip()[:80] or username,
            "token_version": 1,
            "created_at": _now_iso(),
        }
        users[username] = user
        save_users(users)
        return public_user(username, user)


def update_user(
    username: str,
    role: str | None = None,
    display_name: str | None = None,
    password: str | None = None,
) -> dict:
    username = (username or "").strip()
    with _lock:
        users = load_users()
        if username not in users:
            raise UserNotFound("user not found")
        user = users[username]
        if role is not None:
            new_role = _validate_role(role)
            if (
                user.get("role") == "admin"
                and new_role != "admin"
                and _admin_count(users) <= 1
            ):
                raise LastAdminError("cannot remove the last admin")
            if new_role != user.get("role"):
                user["role"] = new_role
                user["token_version"] = int(user.get("token_version") or 0) + 1
        if display_name is not None:
            display_name = str(display_name or username)
            user["display_name"] = display_name.strip()[:80] or username
        if password:
            user["password"] = generate_password_hash(_validate_password(password))
            user["token_version"] = int(user.get("token_version") or 0) + 1
        users[username] = user
        save_users(users)
        return public_user(username, user)


def delete_user(username: str) -> None:
    username = (username or "").strip()
    with _lock:
        users = load_users()
        if username not in users:
            raise UserNotFound("user not found")
        if users[username].get("role") == "admin" and _admin_count(users) <= 1:
            raise LastAdminError("cannot delete the last admin")
        del users[username]
        save_users(users)


def update_account(
    username: str,
    new_username: str | None = None,
    new_password: str | None = None,
    display_name: str | None = None,
) -> tuple[str, dict]:
    """Update the logged-in user's own account and return the new username."""
    with _lock:
        users = load_users()
        if username not in users:
            raise UserNotFound("user not found")
        user = users[username]
        target = username
        if new_username and new_username != username:
            target = _validate_username(new_username)
            if target in users:
                raise UserExists("username already taken")
            users[target] = users.pop(username)
            user = users[target]
            # Old JWTs carry the old sub and are rejected by user lookup.
            user["token_version"] = int(user.get("token_version") or 0) + 1
        if display_name is not None:
            display_name = str(display_name or target)
            user["display_name"] = display_name.strip()[:80] or target
        if new_password:
            user["password"] = generate_password_hash(_validate_password(new_password))
            user["token_version"] = int(user.get("token_version") or 0) + 1
        users[target] = user
        save_users(users)
        return target, user
