"""Admin routes — Agent & Plugin configuration via JSON storage."""

import json
import os
from datetime import UTC, datetime

from flask import jsonify, request

from auth import require_agent_apikey
from core import app, logger

CONF_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "conf")
CONFIG_JSON = os.path.join(CONF_DIR, "agents.json")
RULES_JSON = os.path.join(CONF_DIR, "rules.json")
EXECUTORS_JSON = os.path.join(CONF_DIR, "executors.json")
NOTIFY_JSON = os.path.join(CONF_DIR, "notifications.json")
PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")
BLACKOUTS_JSON = os.path.join(CONF_DIR, "blackouts.json")


# ── Plugin Schemas ──

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")

DEFAULT_SCHEMA = {
    "label": "no label set",
    "description": "no description available",
    "fields": [
        {"key": "sleep", "label": "Interval (s)", "type": "number", "default": 300, "min": 5},
    ],
}


def _get_plugin_schema(name: str) -> dict | None:
    """Read __schema__ from a plugin file using AST (no execution)."""
    import ast
    fpath = os.path.join(PLUGIN_DIR, f"{name}.py")
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__schema__":
                        return ast.literal_eval(node.value)
    except Exception:
        pass
    return None


def _get_all_plugin_names() -> list[str]:
    """Return all plugin names from the plugins directory."""
    if not os.path.isdir(PLUGIN_DIR):
        return []
    names = []
    for fname in sorted(os.listdir(PLUGIN_DIR)):
        if fname.endswith(".py") and not fname.startswith("_"):
            names.append(fname[:-3])
    return names


# ── JSON Config I/O ──

def _load_json_config() -> dict:
    """Load config from agents.json."""
    if os.path.exists(CONFIG_JSON):
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    cfg = {"agents": {}, "groups": {}}
    _save_json_config(cfg)
    return cfg
    return cfg


def _save_json_config(cfg: dict) -> None:
    """Write config to agents.json."""
    with open(CONFIG_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── Routes ──

@app.route("/admin/plugins/schemas", methods=["GET"])
@require_agent_apikey
def admin_plugin_schemas():
    """Return schemas for all plugins, reading from plugin files first."""
    import ast
    plugins_dir = os.path.join(os.path.dirname(__file__), "..", "plugins")
    schemas = {}
    if os.path.isdir(plugins_dir):
        for fname in sorted(os.listdir(plugins_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            mod_name = fname[:-3]
            schema = _get_plugin_schema(mod_name)
            if schema is not None:
                schemas[mod_name] = schema
    # Fallback for plugins without __schema__
    for name in _get_all_plugin_names():
        if name not in schemas:
            schemas[name] = DEFAULT_SCHEMA
    return jsonify(schemas)


@app.route("/admin/agents", methods=["GET"])
@require_agent_apikey
def admin_list_agents():
    """List all agents with groups, plugin configs, and online status."""
    from datetime import datetime, timezone
    from sqlalchemy import func
    from core import SessionLocal
    from db_models import Metrics

    cfg = _load_json_config()
    agents = cfg.get("agents", {})

    # Query last_seen per agent from Metrics
    session = SessionLocal()
    try:
        last_seen_rows = (
            session.query(Metrics.agentid, func.max(Metrics.timestamp).label("last_seen"))
            .group_by(Metrics.agentid)
            .all()
        )
    except Exception:
        last_seen_rows = []
    finally:
        session.close()

    last_seen_map = {row.agentid: row.last_seen for row in last_seen_rows}
    now = datetime.now(timezone.utc)

    result = {}
    for agent_id, agent_data in agents.items():
        data = dict(agent_data)
        last_seen = last_seen_map.get(agent_id)
        data["last_seen"] = last_seen.isoformat() if last_seen else None

        # Compute offline threshold: min sleep across all configured plugins
        plugin_configs = data.get("plugins", {})
        sleeps = []
        for pname, pcfg in plugin_configs.items():
            sleep_val = pcfg.get("sleep")
            if sleep_val:
                sleeps.append(int(sleep_val))
        threshold = min(sleeps) if sleeps else 60

        if last_seen is None:
            data["online"] = False
        else:
            # Make last_seen timezone-aware for comparison
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            elapsed = (now - last_seen).total_seconds()
            data["online"] = elapsed < threshold * 2  # 2x sleep as grace period
        result[agent_id] = data

    return jsonify(result)


@app.route("/admin/agents", methods=["POST"])
@require_agent_apikey
def admin_create_agent():
    """Create a new agent."""
    import secrets
    data = request.get_json(silent=True) or {}
    agent_id = data.get("id", "").strip()
    if not agent_id:
        agent_id = "a_" + secrets.token_hex(4)
    if not agent_id.isalnum():
        return jsonify({"error": "agent id must be alphanumeric"}), 400

    cfg = _load_json_config()
    if agent_id in cfg.get("agents", {}):
        return jsonify({"error": "agent already exists"}), 409

    api_key = data.get("apikey") or secrets.token_hex(8)

    cfg.setdefault("agents", {})[agent_id] = {
        "title": data.get("title", agent_id),
        "groups": data.get("groups", []),
        "apikey": api_key,
        "plugins": {},
    }
    _save_json_config(cfg)
    return jsonify({"status": "created", "agentid": agent_id, "apikey": api_key}), 201


@app.route("/admin/agents/<agentid>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_agent(agentid: str):
    """Delete an agent."""
    cfg = _load_json_config()
    if agentid not in cfg.get("agents", {}):
        return jsonify({"error": "not found"}), 404
    del cfg["agents"][agentid]
    _save_json_config(cfg)
    return jsonify({"status": "deleted"})


@app.route("/admin/agents/<agentid>/enabled", methods=["PUT"])
@require_agent_apikey
def admin_toggle_agent_enabled(agentid: str):
    """Enable or disable an agent."""
    cfg = _load_json_config()
    agents = cfg.get("agents", {})
    if agentid not in agents:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    agents[agentid]["enabled"] = bool(data.get("enabled", True))
    _save_json_config(cfg)
    return jsonify({"status": "updated", "enabled": agents[agentid]["enabled"]})


@app.route("/admin/agents/<agentid>", methods=["PUT"])
@require_agent_apikey
def admin_update_agent(agentid: str):
    """Update agent metadata (title, etc.)."""
    cfg = _load_json_config()
    agents = cfg.get("agents", {})
    if agentid not in agents:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    if "title" in data:
        agents[agentid]["title"] = data["title"]
    _save_json_config(cfg)
    return jsonify({"status": "updated"})


@app.route("/admin/agents/<agentid>/groups", methods=["PUT"])
@require_agent_apikey
def admin_set_agent_groups(agentid: str):
    """Update agent group membership."""
    data = request.get_json(silent=True) or {}
    groups = data.get("groups", [])

    cfg = _load_json_config()
    if agentid not in cfg.get("agents", {}):
        return jsonify({"error": "not found"}), 404
    cfg["agents"][agentid]["groups"] = groups
    _save_json_config(cfg)
    return jsonify({"status": "updated", "groups": groups})


@app.route("/admin/agents/<agentid>/plugins", methods=["GET"])
@require_agent_apikey
def admin_get_agent_plugins(agentid: str):
    """Get plugin config for an agent."""
    cfg = _load_json_config()
    agent = cfg.get("agents", {}).get(agentid)
    if agent is None:
        return jsonify({})
    return jsonify(agent.get("plugins", {}))


@app.route("/admin/agents/<agentid>/plugins/<pluginid>", methods=["PUT"])
@require_agent_apikey
def admin_set_plugin_config(agentid: str, pluginid: str):
    """Update plugin config for an agent."""
    data = request.get_json(silent=True) or {}

    cfg = _load_json_config()
    if agentid not in cfg.get("agents", {}):
        return jsonify({"error": "agent not found"}), 404

    # Validate against schema
    schema = _get_plugin_schema(pluginid) or DEFAULT_SCHEMA
    if schema is None:
        return jsonify({"error": f"unknown plugin: {pluginid}"}), 400

    clean = _validate_against_schema(data, schema)
    cfg["agents"][agentid].setdefault("plugins", {})[pluginid] = clean
    _save_json_config(cfg)
    return jsonify({"status": "updated", "config": clean})


@app.route("/admin/agents/<agentid>/plugins/<pluginid>", methods=["DELETE"])
@require_agent_apikey
def admin_remove_plugin(agentid: str, pluginid: str):
    """Remove a plugin from an agent."""
    cfg = _load_json_config()
    agent = cfg.get("agents", {}).get(agentid)
    if agent is None:
        return jsonify({"error": "not found"}), 404
    agent.get("plugins", {}).pop(pluginid, None)
    _save_json_config(cfg)
    return jsonify({"status": "removed"})


@app.route("/admin/groups", methods=["GET"])
@require_agent_apikey
def admin_list_groups():
    """List all groups with their plugins."""
    cfg = _load_json_config()
    return jsonify(cfg.get("groups", {}))


# ── Rules CRUD ──

RULE_SCHEMA = {
    "fields": [
        {"key": "id", "label": "ID", "type": "string"},
        {"key": "enabled", "label": "Enabled", "type": "boolean", "default": True},
        {"key": "description", "label": "Description", "type": "string", "default": ""},
        {"key": "pluginid", "label": "Plugin", "type": "string"},
        {"key": "metric", "label": "Metric", "type": "string"},
        {"key": "condition", "label": "Condition", "type": "select", "options": ["gt", "ge", "lt", "le", "eq", "ne"]},
        {"key": "threshold", "label": "Threshold", "type": "number"},
        {"key": "scope", "label": "Scope", "type": "select", "options": ["single", "moving_avg", "count_ratio"]},
        {"key": "window_size", "label": "Window (N measurements)", "type": "number", "default": 10, "optional": True},
        {"key": "min_violations", "label": "Min. violations", "type": "number", "default": 1, "optional": True},
        {"key": "severity", "label": "Severity", "type": "select", "options": ["warning", "critical"]},
        {"key": "fire", "label": "Fire mode", "type": "select", "options": ["single", "multi", "replace"]},
        {"key": "notifications", "label": "Notifications", "type": "array:string", "default": []},
        {"key": "executors", "label": "Executors", "type": "array:string", "default": []},
        {"key": "agents_mode", "label": "Mode", "type": "select", "options": ["exclude", "include"], "default": "exclude"},
        {"key": "agents", "label": "Agents", "type": "agents", "default": [], "optional": True},
    ],
}


def _load_rules() -> dict:
    if os.path.exists(RULES_JSON):
        with open(RULES_JSON, encoding="utf-8") as f:
            return json.load(f)
    rules_map = {}
    _save_rules(rules_map)
    return rules_map


def _save_rules(rules_map: dict) -> None:
    with open(RULES_JSON, "w", encoding="utf-8") as f:
        json.dump(rules_map, f, indent=2, ensure_ascii=False)


@app.route("/admin/rules/schema", methods=["GET"])
@require_agent_apikey
def admin_rules_schema():
    """Return the rule schema for the UI form."""
    return jsonify(RULE_SCHEMA)


@app.route("/admin/rules", methods=["GET"])
@require_agent_apikey
def admin_list_rules():
    """List all rules."""
    return jsonify(_load_rules())


@app.route("/admin/rules/<rule_id>", methods=["PUT"])
@require_agent_apikey
def admin_update_rule(rule_id: str):
    """Create or update a rule."""
    data = request.get_json(silent=True) or {}
    data["id"] = rule_id
    rules_map = _load_rules()
    rules_map[rule_id] = data
    _save_rules(rules_map)
    return jsonify({"status": "saved", "rule": data})


@app.route("/admin/rules/<rule_id>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_rule(rule_id: str):
    """Delete a rule."""
    rules_map = _load_rules()
    if rule_id not in rules_map:
        return jsonify({"error": "not found"}), 404
    del rules_map[rule_id]
    _save_rules(rules_map)
    return jsonify({"status": "deleted"})


@app.route("/admin/groups/<groupid>", methods=["PUT"])
@require_agent_apikey
def admin_set_group(groupid: str):
    """Set plugins for a group."""
    data = request.get_json(silent=True) or {}
    plugins = data.get("plugins", [])

    cfg = _load_json_config()
    cfg.setdefault("groups", {})[groupid] = plugins
    _save_json_config(cfg)
    return jsonify({"status": "updated"})


@app.route("/admin/groups/<groupid>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_group(groupid: str):
    """Delete a group."""
    cfg = _load_json_config()
    cfg.get("groups", {}).pop(groupid, None)
    _save_json_config(cfg)
    return jsonify({"status": "deleted"})


# ── Executors CRUD ──

def _load_executors() -> dict:
    if os.path.exists(EXECUTORS_JSON):
        with open(EXECUTORS_JSON, encoding="utf-8") as f:
            return json.load(f)
    exec_map = {}
    _save_executors(exec_map)
    return exec_map


def _save_executors(exec_map: dict) -> None:
    with open(EXECUTORS_JSON, "w", encoding="utf-8") as f:
        json.dump(exec_map, f, indent=2, ensure_ascii=False)


@app.route("/admin/executors", methods=["GET"])
@require_agent_apikey
def admin_list_executors():
    return jsonify(_load_executors())


@app.route("/admin/executors/<exec_id>", methods=["PUT"])
@require_agent_apikey
def admin_save_executor(exec_id: str):
    data = request.get_json(silent=True) or {}
    data["id"] = exec_id
    exec_map = _load_executors()
    exec_map[exec_id] = data
    _save_executors(exec_map)
    return jsonify({"status": "saved"})


@app.route("/admin/executors/<exec_id>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_executor(exec_id: str):
    exec_map = _load_executors()
    if exec_id not in exec_map:
        return jsonify({"error": "not found"}), 404
    del exec_map[exec_id]
    _save_executors(exec_map)
    return jsonify({"status": "deleted"})


# ── Notifications CRUD ──

NOTIFY_SCHEMA = {
    "fields": [
        {"key": "id", "label": "ID", "type": "string"},
        {"key": "title", "label": "Title", "type": "string", "default": ""},
        {"key": "enabled", "label": "Enabled", "type": "boolean", "default": True},
        {"key": "type", "label": "Type", "type": "select", "options": ["email", "web_push", "ntfy", "twilio_call"]},
        {"key": "to", "label": "Recipient", "type": "string"},
        {"key": "from", "label": "Sender", "type": "string"},
        {"key": "server", "label": "SMTP Server", "type": "string"},
        {"key": "port", "label": "Port", "type": "number", "default": 587},
        {"key": "user", "label": "SMTP User", "type": "string"},
        {"key": "password", "label": "Password (or env NOTIFY_EMAIL_PASSWORD)", "type": "string", "optional": True},
        {"key": "use_tls", "label": "Use TLS", "type": "boolean", "default": True},
        {"key": "vapid_public_key", "label": "VAPID Public Key", "type": "string", "optional": True},
        {"key": "vapid_private_key", "label": "VAPID Private Key", "type": "string", "optional": True},
        {"key": "vapid_subject", "label": "VAPID Subject (mailto:...)", "type": "string", "default": "mailto:admin@localhost", "optional": True},
        {"key": "ntfy_url", "label": "ntfy Server URL", "type": "string", "default": "https://ntfy.sh", "optional": True},
        {"key": "ntfy_topic", "label": "ntfy Topic", "type": "string", "optional": True},
        {"key": "ntfy_access_token", "label": "ntfy Access Token", "type": "string", "optional": True},
        {"key": "twilio_account_sid", "label": "Twilio Account SID", "type": "string", "optional": True},
        {"key": "twilio_auth_token", "label": "Twilio Auth Token", "type": "string", "optional": True},
        {"key": "twilio_call_from", "label": "Call From Number", "type": "string", "optional": True},
        {"key": "twilio_call_to", "label": "Call To Number", "type": "string", "optional": True},
    ],
}


def _load_notify() -> dict:
    if os.path.exists(NOTIFY_JSON):
        with open(NOTIFY_JSON, encoding="utf-8") as f:
            return json.load(f)
    notify_map = {}
    _save_notify(notify_map)
    return notify_map


def _save_notify(notify_map: dict) -> None:
    with open(NOTIFY_JSON, "w", encoding="utf-8") as f:
        json.dump(notify_map, f, indent=2, ensure_ascii=False)


@app.route("/admin/notifications", methods=["GET"])
@require_agent_apikey
def admin_list_notify():
    return jsonify(_load_notify())


@app.route("/admin/notifications/schema", methods=["GET"])
@require_agent_apikey
def admin_notify_schema():
    return jsonify(NOTIFY_SCHEMA)


@app.route("/admin/notifications/<notify_id>", methods=["PUT"])
@require_agent_apikey
def admin_save_notify(notify_id: str):
    data = request.get_json(silent=True) or {}
    data["id"] = notify_id
    notify_map = _load_notify()
    notify_map[notify_id] = data
    _save_notify(notify_map)
    return jsonify({"status": "saved"})


@app.route("/admin/notifications/<notify_id>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_notify(notify_id: str):
    notify_map = _load_notify()
    if notify_id not in notify_map:
        return jsonify({"error": "not found"}), 404
    del notify_map[notify_id]
    _save_notify(notify_map)
    return jsonify({"status": "deleted"})


@app.route("/admin/notifications/test", methods=["POST"])
@require_agent_apikey
def admin_test_notify():
    """Send a test notification."""
    data = request.get_json(silent=True) or {}
    target_type = data.get("type")
    if target_type == "email":
        from notifications import send_email_notification
        subject = f"[pymon] Test notification from {data.get('id', 'unknown')}"
        body = "This is a test notification from pymon.\n\nIf you receive this, your email config works."
        try:
            send_email_notification(data, subject, body)
            return jsonify({"status": "test sent"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif target_type == "web_push":
        from services.web_push import send_push_notification
        try:
            send_push_notification(
                "[pymon] Test",
                "Web Push test notification from pymon",
                tag="pymon-test",
                private_key=data.get("vapid_private_key") or None,
                public_key=data.get("vapid_public_key") or None,
                subject=data.get("vapid_subject") or None,
            )
            return jsonify({"status": "test sent"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif target_type == "ntfy":
        import urllib.request
        ntfy_url = (data.get("ntfy_url") or "https://ntfy.sh").rstrip("/")
        topic = data.get("ntfy_topic")
        if not topic:
            return jsonify({"error": "ntfy_topic required"}), 400
        token = data.get("ntfy_access_token") or None
        import json
        payload = json.dumps({
            "topic": topic,
            "title": "[pymon] Test",
            "message": "Test notification from pymon",
            "tags": ["test"],
        }).encode()
        req = urllib.request.Request(
            ntfy_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            urllib.request.urlopen(req, timeout=10)
            return jsonify({"status": "test sent"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif target_type == "twilio_call":
        try:
            from twilio.rest import Client
        except ImportError:
            return jsonify({"error": "twilio package not installed"}), 500
        sid = data.get("twilio_account_sid")
        token = data.get("twilio_auth_token")
        call_from = data.get("twilio_call_from")
        call_to = data.get("twilio_call_to")
        if not all([sid, token, call_from, call_to]):
            return jsonify({"error": "twilio_account_sid, twilio_auth_token, twilio_call_from, twilio_call_to required"}), 400
        try:
            client = Client(sid, token)
            client.calls.create(
                twiml="<Response><Say>This is a test call from pymon.</Say></Response>",
                to=call_to,
                from_=call_from,
            )
            return jsonify({"status": "test sent"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": f"unknown type: {target_type}"}), 400


# ── Plugin Management ──

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")
PLUGIN_META_JSON = os.path.join(CONF_DIR, "plugins.json")


def _load_plugin_meta() -> dict:
    if os.path.exists(PLUGIN_META_JSON):
        with open(PLUGIN_META_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_plugin_meta(meta: dict) -> None:
    with open(PLUGIN_META_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


@app.route("/admin/plugins", methods=["GET"])
@require_agent_apikey
def admin_list_plugins():
    """List all available plugins with metadata."""
    plugins = []
    if not os.path.exists(PLUGIN_DIR):
        return jsonify([])
    for fname in sorted(os.listdir(PLUGIN_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        name = fname[:-3]
        fpath = os.path.join(PLUGIN_DIR, fname)
        try:
            size = os.path.getsize(fpath)
            with open(fpath, encoding="utf-8") as f:
                first_line = f.readline().strip()
            desc = ""
            # Extract description from docstring or shebang
            if first_line.startswith("#!"):
                with open(fpath, encoding="utf-8") as f:
                    f.readline()
                    second = f.readline().strip()
                    if second.startswith('"""') or second.startswith("'''"):
                        desc = second.strip('"\' ')
            elif first_line.startswith('"""') or first_line.startswith("'''"):
                desc = first_line.strip('"\' ')
            schema = _get_plugin_schema(name) or DEFAULT_SCHEMA
            meta = _load_plugin_meta()
            pm = meta.get(name, {})
            plugins.append({
                "name": name,
                "label": schema.get("label", name),
                "description": schema.get("description", desc),
                "size": size,
                "enabled": pm.get("enabled", True),
            })
        except Exception:
            plugins.append({"name": name, "label": name, "description": "", "size": 0})
    return jsonify(plugins)


@app.route("/admin/plugins/template", methods=["GET"])
@require_agent_apikey
def admin_plugin_template():
    """Return the plugin template source code."""
    tpath = os.path.join(PLUGIN_DIR, "_template.py")
    if not os.path.exists(tpath):
        return jsonify({"error": "template not found"}), 404
    with open(tpath, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/plain"}


@app.route("/admin/plugins/<name>/source", methods=["GET"])
@require_agent_apikey
def admin_get_plugin_source(name: str):
    """Get plugin source code."""
    fpath = os.path.join(PLUGIN_DIR, f"{name}.py")
    if not os.path.exists(fpath):
        return jsonify({"error": "not found"}), 404
    with open(fpath, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/plain"}


@app.route("/admin/plugins/<name>/source", methods=["PUT"])
@require_agent_apikey
def admin_save_plugin_source(name: str):
    """Update plugin source code."""
    data = request.get_data(as_text=True)
    if not data:
        return jsonify({"error": "empty source"}), 400
    fpath = os.path.join(PLUGIN_DIR, f"{name}.py")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(data)
    return jsonify({"status": "saved"}), 200


@app.route("/admin/plugins/<name>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_plugin(name: str):
    """Delete a plugin file."""
    fpath = os.path.join(PLUGIN_DIR, f"{name}.py")
    if not os.path.exists(fpath):
        return jsonify({"error": "not found"}), 404
    os.remove(fpath)
    return jsonify({"status": "deleted"}), 200


@app.route("/admin/plugins/<name>/enabled", methods=["PUT"])
@require_agent_apikey
def admin_toggle_plugin(name: str):
    """Toggle plugin enabled state."""
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", True)
    meta = _load_plugin_meta()
    meta[name] = {"enabled": enabled}
    _save_plugin_meta(meta)
    return jsonify({"status": "updated", "enabled": enabled})


@app.route("/admin/plugins/check", methods=["POST"])
@require_agent_apikey
def admin_check_plugin():
    """Check Python source for syntax errors and optionally run ruff."""
    data = request.get_data(as_text=True)
    if not data:
        return jsonify({"error": "empty source"}), 400

    errors = []
    # 1. compile check
    try:
        compile(data, "<plugin>", "exec")
    except SyntaxError as e:
        errors.append({"type": "syntax", "line": e.lineno, "msg": e.msg})

    # 2. ruff check (if available)
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(data)
        tmp = f.name
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "E,F", "--output-format", "json", tmp],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout:
            import json as _json
            for issue in _json.loads(result.stdout):
                errors.append({
                    "type": "ruff",
                    "line": issue.get("location", {}).get("row"),
                    "msg": f"{issue.get('code', '?')} {issue.get('message', '')}",
                })
    except FileNotFoundError:
        pass  # ruff not installed
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return jsonify({"ok": len(errors) == 0, "errors": errors})


# ── Schema Helpers ──

def _validate_against_schema(data: dict, schema: dict) -> dict:
    """Strip unknown fields and apply defaults from schema."""
    valid_keys = {f["key"] for f in schema.get("fields", [])}
    result = {}
    for field in schema.get("fields", []):
        key = field["key"]
        if key in data:
            result[key] = data[key]
        elif "default" in field:
            # For array types, copy default to avoid mutation
            default = field["default"]
            if isinstance(default, list):
                result[key] = list(default)
            else:
                result[key] = default
    return result


# ── Blackouts CRUD ──

def _load_blackouts() -> dict:
    if not os.path.exists(BLACKOUTS_JSON):
        return {}
    with open(BLACKOUTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _save_blackouts(data: dict) -> None:
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(BLACKOUTS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


BLACKOUT_SCHEMA = {
    "fields": [
        {"key": "id", "label": "ID", "type": "string"},
        {"key": "enabled", "label": "Enabled", "type": "boolean", "default": True},
        {"key": "title", "label": "Title", "type": "string", "default": ""},
        {"key": "weekdays", "label": "Weekdays", "type": "weekdays", "default": []},
        {"key": "start_time", "label": "Start time", "type": "string", "default": "00:00"},
        {"key": "end_time", "label": "End time", "type": "string", "default": "23:59"},
        {"key": "target_mode", "label": "Target mode", "type": "select", "options": ["rules", "agents"], "default": "rules"},
        {"key": "targets", "label": "Targets", "type": "targets", "default": []},
        {"key": "mode", "label": "Blackout mode", "type": "select", "options": ["no_alarms", "no_notifications"], "default": "no_alarms"},
    ],
}


@app.route("/admin/blackouts", methods=["GET"])
@require_agent_apikey
def admin_list_blackouts():
    return jsonify(_load_blackouts())


@app.route("/admin/blackouts/schema", methods=["GET"])
@require_agent_apikey
def admin_blackout_schema():
    return jsonify(BLACKOUT_SCHEMA)


@app.route("/admin/blackouts/<blackout_id>", methods=["PUT"])
@require_agent_apikey
def admin_save_blackout(blackout_id: str):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no data"}), 400

    item = _validate_against_schema(data, BLACKOUT_SCHEMA)
    item["id"] = blackout_id

    blackouts = _load_blackouts()
    blackouts[blackout_id] = item
    _save_blackouts(blackouts)
    return jsonify({"status": "saved", "id": blackout_id})


@app.route("/admin/blackouts/<blackout_id>", methods=["DELETE"])
@require_agent_apikey
def admin_delete_blackout(blackout_id: str):
    blackouts = _load_blackouts()
    if blackout_id in blackouts:
        del blackouts[blackout_id]
        _save_blackouts(blackouts)
    return jsonify({"status": "deleted"})