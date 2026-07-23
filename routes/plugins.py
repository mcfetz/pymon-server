from core import app, logger
from flask import request, jsonify
import json
import os
import hashlib

from auth import require_agent_apikey
from config import CONF_DIR, PLUGINS_DIR


def get_assigned_plugins_for_agentid(agentid: str) -> list:
    try:
        agents_json = os.path.join(CONF_DIR, "agents.json")
        if os.path.exists(agents_json):
            with open(agents_json) as f:
                cfg = json.load(f)
            agent = cfg.get("agents", {}).get(agentid)
            if not agent:
                return []
            groups = cfg.get("groups", {})
            assigned = set()
            for g in agent.get("groups", []):
                grp = groups.get(g, [])
                plugins_list = grp.get("plugins", grp) if isinstance(grp, dict) else grp
                for p in plugins_list:
                    assigned.add(p)
            for p in agent.get("plugins", {}):
                assigned.add(p)
            return list(assigned)
    except Exception as e:
        logger.error("error loading agent config: %s", e)
    return []


@app.route("/plugins", methods=["GET"])
@require_agent_apikey
def plugins():
    """
    List plugins assigned to an agent.
    ---
    tags:
      - plugins
    parameters:
      - in: header
        name: agentid
        required: true
        schema:
          type: string
        description: Authenticated agent identifier
      - in: header
        name: X-API-Key
        required: true
        schema:
          type: string
        description: API key for the agent
    responses:
      200:
        description: List of plugin ids assigned to the agent
        content:
          application/json:
            schema:
              type: array
              items:
                type: string
      400:
        description: Missing agentid header
      401:
        description: Invalid or missing API key
      500:
        description: Error while loading configuration
    """
    assigned_plugins = get_assigned_plugins_for_agentid(request.agentid)

    # If no groups/plugins are defined, an empty set or e.g. all plugins could be returned.
    # Here: return the filtered plugins according to the configuration
    return jsonify(assigned_plugins), 200


@app.route("/plugins/<name>", methods=["GET"])
@require_agent_apikey
def get_plugin(name):
    """
    Get plugin source code.
    ---
    tags:
      - plugins
    parameters:
      - in: path
        name: name
        required: true
        schema:
          type: string
        description: Plugin id (file name without .py)
      - in: header
        name: agentid
        required: true
        schema:
          type: string
        description: Authenticated agent identifier
      - in: header
        name: X-API-Key
        required: true
        schema:
          type: string
        description: API key for the agent
    responses:
      200:
        description: Plugin source code
        content:
          text/plain:
            schema:
              type: string
      400:
        description: Missing agentid header
      401:
        description: Invalid or missing API key
      404:
        description: Plugin not found or not assigned
      500:
        description: Error while reading plugin file
    """
    assigned_plugins = get_assigned_plugins_for_agentid(request.agentid)

    if name not in assigned_plugins:
        return jsonify({"error": f"Plugin '{name}' not assigned to agent {request.agentid}"}), 404

    # Build the path to the Python script file in the 'plugins' folder
    plugin_path = os.path.join(PLUGINS_DIR, f"{name}.py")

    # Check if the file exists and is readable
    if not os.path.exists(plugin_path):
        return jsonify({"error": f"Plugin '{name}' nicht gefunden."}), 404

    try:
        with open(plugin_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return jsonify({"error": f"Fehler beim Lesen des Plugins: {e!s}"}), 500

    # Return the content as plain text
    return content, 200, {"Content-Type": "text/plain"}


@app.route("/plugins/<name>/version", methods=["GET"])
@require_agent_apikey
def get_plugin_version(name):
    """
    Get plugin version hash for self-update checks.
    ---
    tags:
      - plugins
    parameters:
      - in: path
        name: name
        required: true
        schema:
          type: string
        description: Plugin id (file name without .py)
    responses:
      200:
        description: Plugin version info
        content:
          application/json:
            schema:
              type: object
              properties:
                name:
                  type: string
                hash:
                  type: string
      404:
        description: Plugin not found
    """
    plugin_path = os.path.join(PLUGINS_DIR, f"{name}.py")
    if not os.path.exists(plugin_path):
        return jsonify({"error": "Plugin not found"}), 404
    try:
        with open(plugin_path, "rb") as f:
            content = f.read()
        h = hashlib.sha256(content).hexdigest()
        return jsonify({"name": name, "hash": h}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/plugins/<name>/config", methods=["GET"])
@require_agent_apikey
def get_plugin_config(name):
    """
    Get plugin configuration for an agent.
    ---
    tags:
      - plugins
    parameters:
      - in: path
        name: name
        required: true
        schema:
          type: string
        description: Plugin id
      - in: header
        name: agentid
        required: true
        schema:
          type: string
        description: Authenticated agent identifier
      - in: header
        name: X-API-Key
        required: true
        schema:
          type: string
        description: API key for the agent
    responses:
      200:
        description: Plugin configuration for the agent
        content:
          application/json:
            schema:
              type: object
      400:
        description: Missing agentid header
      401:
        description: Invalid or missing API key
      403:
        description: Plugin not assigned to this agent
      500:
        description: Error while loading configuration
    """
    try:
        agents_json = os.path.join(CONF_DIR, "agents.json")
        if not os.path.exists(agents_json):
            return jsonify({"error": "no config"}), 500
        with open(agents_json) as f:
            cfg = json.load(f)

        agent = cfg.get("agents", {}).get(request.agentid)
        if not agent:
            return jsonify({"error": "agent not found"}), 404

        # Collect all assigned plugins for this agent
        assigned = set()
        for g in agent.get("groups", []):
            for p in cfg.get("groups", {}).get(g, []):
                assigned.add(p)
        for p in agent.get("plugins", {}):
            assigned.add(p)

        if name not in assigned:
            return jsonify({"error": "plugin not assigned to this agent"}), 403

        # Return plugin config from the agent's plugin config
        plugin_config = agent.get("plugins", {}).get(name, {})
        return jsonify(plugin_config), 200
    except Exception as e:
        logger.error("Error loading plugin config: %s", e)
        return jsonify({"error": "error loading config"}), 500

    # Return the configuration as a dictionary
    return jsonify(plugin_config), 200
