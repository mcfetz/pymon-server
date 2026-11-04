"""Admin-only user management endpoints."""

from flask import jsonify, request

from audit import record_audit
from auth import require_admin, require_agent_apikey
from core import app
from users import (
    LastAdminError,
    UserError,
    UserExists,
    UserNotFound,
    create_user,
    delete_user,
    get_user,
    list_public_users,
    public_user,
    update_user,
)


def _error(exc):
    if isinstance(exc, UserNotFound):
        return jsonify({"error": "user not found"}), 404
    if isinstance(exc, UserExists):
        return jsonify({"error": "user already exists"}), 409
    if isinstance(exc, LastAdminError):
        return jsonify({"error": str(exc)}), 409
    return jsonify({"error": str(exc)}), 400


@app.route("/admin/users", methods=["GET"])
@require_agent_apikey
@require_admin
def admin_list_users():
    return jsonify(list_public_users()), 200


@app.route("/admin/users", methods=["POST"])
@require_agent_apikey
@require_admin
def admin_create_user():
    data = request.get_json(silent=True) or {}
    try:
        user = create_user(
            data.get("username", ""),
            data.get("password", ""),
            role=data.get("role", "user"),
            display_name=data.get("display_name", ""),
        )
    except UserError as exc:
        return _error(exc)
    record_audit(
        "user.create",
        "user",
        user["username"],
        summary=f"created user {user['username']} ({user['role']})",
        details={"role": user["role"], "display_name": user["display_name"]},
    )
    return jsonify(user), 201


@app.route("/admin/users/<username>", methods=["PUT"])
@require_agent_apikey
@require_admin
def admin_update_user(username: str):
    data = request.get_json(silent=True) or {}
    before = get_user(username)
    if not before:
        return jsonify({"error": "user not found"}), 404
    role = data.get("role") if "role" in data else None
    display_name = data.get("display_name") if "display_name" in data else None
    password = data.get("password") or None
    changed = []
    if role is not None and role != before.get("role"):
        changed.append("role")
    if display_name is not None and display_name != before.get("display_name"):
        changed.append("display_name")
    if password:
        changed.append("password")
    try:
        user = update_user(
            username,
            role=role,
            display_name=display_name,
            password=password,
        )
    except UserError as exc:
        return _error(exc)
    record_audit(
        "user.update",
        "user",
        username,
        summary=f"updated user {username} ({', '.join(changed) or 'no changes'})",
        details={"changed_fields": changed, "role": user["role"]},
    )
    return jsonify(user), 200


@app.route("/admin/users/<username>", methods=["DELETE"])
@require_agent_apikey
@require_admin
def admin_delete_user(username: str):
    if username == getattr(request, "frontend_user", None):
        return jsonify({"error": "cannot delete your own account"}), 400
    before = get_user(username)
    if not before:
        return jsonify({"error": "user not found"}), 404
    try:
        delete_user(username)
    except UserError as exc:
        return _error(exc)
    record_audit(
        "user.delete",
        "user",
        username,
        summary=f"deleted user {username} ({before.get('role')})",
        details={"role": before.get("role")},
    )
    return jsonify({"status": "deleted"}), 200


@app.route("/admin/users/<username>", methods=["GET"])
@require_agent_apikey
@require_admin
def admin_get_user(username: str):
    user = get_user(username)
    if not user:
        return jsonify({"error": "user not found"}), 404
    return jsonify(public_user(username, user)), 200
