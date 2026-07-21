"""Admin routes — Agent & Plugin configuration via JSON storage."""

import json
import os
from datetime import UTC, datetime

import toml
from flask import jsonify, request

from auth import require_agent_apikey
from core import app, logger

CONF_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "conf")
CONFIG_JSON = os.path.join(CONF_DIR, "agents.json")
RULES_JSON = os.path.join(CONF_DIR, "rules.json")
EXECUTORS_JSON = os.path.join(CONF_DIR, "executors.json")


# ── Plugin Schemas ──

PLUGIN_SCHEMAS = {
    "cpu": {
        "label": "CPU",
        "description": "CPU-Auslastung in Prozent",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 30, "min": 5},
        ],
    },
    "ram": {
        "label": "RAM",
        "description": "Arbeitsspeicher und Swap-Auslastung",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 30, "min": 5},
        ],
    },
    "disk_usage": {
        "label": "Festplatte",
        "description": "Speicherbelegung pro Partition",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 60, "min": 5},
            {"key": "excludes", "label": "Ausgeschlossene Mountpoints", "type": "array:string", "default": []},
        ],
    },
    "network": {
        "label": "Netzwerk",
        "description": "Netzwerk-IO und TCP-Verbindungen",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 30, "min": 5},
        ],
    },
    "ping": {
        "label": "Ping",
        "description": "ICMP Ping zu konfigurierten Hosts",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 60, "min": 5},
            {"key": "hosts", "label": "Hosts", "type": "array:string", "default": []},
        ],
    },
    "http_check": {
        "label": "HTTP Check",
        "description": "HTTP/HTTPS Statuscode und Inhalt prüfen",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 60, "min": 5},
            {"key": "timeout", "label": "Timeout (s)", "type": "number", "default": 5, "min": 1},
            {"key": "urls", "label": "URLs", "type": "array:object", "default": [], "fields": [
                {"key": "name", "label": "Name", "type": "string"},
                {"key": "url", "label": "URL", "type": "string"},
                {"key": "expected_string", "label": "Erwarteter Text", "type": "string", "optional": True},
            ]},
        ],
    },
    "host": {
        "label": "Host",
        "description": "Systeminformationen (Hostname, OS, CPU, RAM)",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 300, "min": 5},
        ],
    },
    "services": {
        "label": "Dienste",
        "description": "systemd-Service-Status prüfen",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 60, "min": 5},
            {"key": "services", "label": "Service-Namen", "type": "array:string", "default": []},
        ],
    },
    "temperature": {
        "label": "Temperatur",
        "description": "Hardware-Sensor-Temperaturen",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 60, "min": 5},
        ],
    },
    "cert_valid": {
        "label": "TLS Zertifikat",
        "description": "SSL-Zertifikatsgültigkeit prüfen",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 86400, "min": 300},
            {"key": "timeout", "label": "Timeout (s)", "type": "number", "default": 5, "min": 1},
            {"key": "urls", "label": "HTTPS-URLs", "type": "array:string", "default": []},
        ],
    },
    "docker_host": {
        "label": "Docker",
        "description": "Docker-Host-Statistiken",
        "fields": [
            {"key": "sleep", "label": "Intervall (s)", "type": "number", "default": 60, "min": 5},
            {"key": "base_url", "label": "Docker Socket URL", "type": "string", "default": "", "optional": True},
        ],
    },
    "plugin_base": None,
}


# ── JSON Config I/O ──

def _load_json_config() -> dict:
    """Load config from agents.json, fall back to TOML on first run."""
    if os.path.exists(CONFIG_JSON):
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)

    # First run: migrate from TOML
    cfg = {"agents": {}, "groups": {}, "_meta": {"migrated_from_toml": True, "created": datetime.now(UTC).isoformat()}}

    # Read config.toml for groups + agent->group mapping
    try:
        toml_cfg = toml.load(os.path.join(CONF_DIR, "config.toml"))
        cfg["groups"] = toml_cfg.get("groups", {})
        agents_section = toml_cfg.get("agents", {})
    except Exception:
        agents_section = {}

    # Read agents.toml for plugin configs
    try:
        agents_toml = toml.load(os.path.join(CONF_DIR, "agents.toml"))
    except Exception:
        agents_toml = {}

    # Read apikeys.toml
    try:
        apikeys = toml.load(os.path.join(CONF_DIR, "apikeys.toml"))
    except Exception:
        apikeys = {}

    for agent_id in list(agents_section.keys()) + [k for k in apikeys if k != "admin"]:
        agent_data = {
            "groups": agents_section.get(agent_id, []),
            "apikey": apikeys.get(agent_id, {}).get("key", ""),
            "plugins": {},
        }
        # Add description if available
        desc = apikeys.get(agent_id, {}).get("description", "")
        if desc:
            agent_data["description"] = desc

        # Migrate plugin configs
        agent_plugin_cfg = agents_toml.get(agent_id, {})
        for plugin_name, schema in PLUGIN_SCHEMAS.items():
            if schema is None:
                continue
            if plugin_name in agent_plugin_cfg:
                agent_data["plugins"][plugin_name] = dict(agent_plugin_cfg[plugin_name])

        cfg["agents"][agent_id] = agent_data

    _save_json_config(cfg)
    return cfg


def _save_json_config(cfg: dict) -> None:
    """Write config to agents.json."""
    with open(CONFIG_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── Routes ──

@app.route("/admin/plugins/schemas", methods=["GET"])
@require_agent_apikey
def admin_plugin_schemas():
    """Return schemas for all plugins."""
    return jsonify({k: v for k, v in PLUGIN_SCHEMAS.items() if v is not None})


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
    data = request.get_json(silent=True) or {}
    agent_id = data.get("id", "").strip()
    if not agent_id:
        return jsonify({"error": "agent id required"}), 400
    if not agent_id.isalnum():
        return jsonify({"error": "agent id must be alphanumeric"}), 400

    cfg = _load_json_config()
    if agent_id in cfg.get("agents", {}):
        return jsonify({"error": "agent already exists"}), 409

    import secrets
    api_key = data.get("apikey") or secrets.token_hex(8)

    cfg.setdefault("agents", {})[agent_id] = {
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
    schema = PLUGIN_SCHEMAS.get(pluginid)
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
        {"key": "id", "label": "Rule-ID", "type": "string"},
        {"key": "enabled", "label": "Aktiviert", "type": "boolean", "default": True},
        {"key": "description", "label": "Beschreibung", "type": "string", "default": ""},
        {"key": "pluginid", "label": "Plugin", "type": "string"},
        {"key": "metric", "label": "Metrik", "type": "string"},
        {"key": "condition", "label": "Bedingung", "type": "select", "options": ["gt", "ge", "lt", "le", "eq", "ne"]},
        {"key": "threshold", "label": "Schwellwert", "type": "number"},
        {"key": "scope", "label": "Scope", "type": "select", "options": ["single", "moving_avg", "count_ratio"]},
        {"key": "window_size", "label": "Fenster (N Messungen)", "type": "number", "default": 10, "optional": True},
        {"key": "min_violations", "label": "Min. Verletzungen", "type": "number", "default": 1, "optional": True},
        {"key": "severity", "label": "Severity", "type": "select", "options": ["warning", "critical"]},
        {"key": "fire", "label": "Fire-Modus", "type": "select", "options": ["single", "multi"]},
        {"key": "notifications", "label": "Benachrichtigungen", "type": "array:string", "default": []},
        {"key": "executors", "label": "Executors", "type": "array:string", "default": []},
    ],
}


def _load_rules() -> dict:
    if os.path.exists(RULES_JSON):
        with open(RULES_JSON, encoding="utf-8") as f:
            return json.load(f)
    # First run: migrate from rules.toml
    rules_map = {}
    try:
        toml_rules = toml.load(os.path.join(CONF_DIR, "rules.toml"))
        for i, r in enumerate(toml_rules.get("rule", [])):
            rid = r.get("id", f"rule_{i}")
            rules_map[rid] = {
                "id": rid,
                "enabled": r.get("enabled", True),
                "description": r.get("description", ""),
                "pluginid": r.get("pluginid", ""),
                "metric": r.get("metric", ""),
                "condition": r.get("condition", "gt"),
                "threshold": float(r.get("threshold", 0)),
                "scope": r.get("scope", "single"),
                "window_size": r.get("window_size"),
                "min_violations": r.get("min_violations"),
                "severity": r.get("severity", "warning"),
                "fire": r.get("fire", "single"),
                "notifications": r.get("notifications", []),
                "executors": r.get("executors", []),
            }
    except Exception:
        pass
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
    try:
        toml_data = toml.load(os.path.join(CONF_DIR, "executors.toml"))
        for eid, econf in toml_data.get("executors", {}).items():
            exec_map[eid] = {"id": eid, "command": econf.get("command", "")}
    except Exception:
        pass
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