from core import app, logger
from flask import request, jsonify
import toml
import os


@app.route("/plugins", methods=["GET"])
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
        description: Agent identifier
    responses:
      200:
        description: List of plugin ids assigned to the agent
        content:
          application/json:
            schema:
              type: array
              items:
                type: string
      500:
        description: Error while loading configuration
    """
    agentid = request.headers.get("agentid", None)

    # Load configuration from config.toml
    try:
        config = toml.load("conf/config.toml")
    except Exception as e:
        return jsonify({"error": f"Fehler beim Laden der Konfiguration: {e!s}"}), 500

    # Determine the groups of the agent (if defined)
    agent_groups = config.get("agents", {}).get(agentid, [])

    # Collect all plugins that are assigned to the agent's groups
    assigned_plugins = set()
    groups_config = config.get("groups", {})
    for group in agent_groups:
        plugins_for_group = groups_config.get(group, [])
        assigned_plugins.update(plugins_for_group)

    # If no groups/plugins are defined, an empty set or e.g. all plugins could be returned.
    # Here: return the filtered plugins according to the configuration
    return jsonify(list(assigned_plugins)), 200


@app.route("/plugins/<name>", methods=["GET"])
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
    responses:
      200:
        description: Plugin source code
        content:
          text/plain:
            schema:
              type: string
      404:
        description: Plugin not found
      500:
        description: Error while reading plugin file
    """
    # Build the path to the Python script file in the 'plugins' folder
    plugin_path = os.path.join("plugins", f"{name}.py")

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


@app.route("/plugins/<name>/config", methods=["GET"])
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
        description: Agent identifier
    responses:
      200:
        description: Plugin configuration for the agent
        content:
          application/json:
            schema:
              type: object
      400:
        description: Missing agentid header
      500:
        description: Error while loading configuration
    """
    # Get agentid from HTTP header
    agentid = request.headers.get("agentid", None)
    if not agentid:
        return jsonify({"error": "agentid header missing"}), 400

    try:
        # Load the contents of the agents.toml file
        agents_config = toml.load("conf/agents.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der agents.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    # Load global config to determine which plugins are assigned to the agent
    try:
        global_config = toml.load("conf/config.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der config.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    # Determine the groups of the agent (if defined)
    agent_groups = global_config.get("agents", {}).get(agentid, [])

    # Collect all plugins that are assigned to the agent's groups
    assigned_plugins = set()
    groups_config = global_config.get("groups", {})
    for group in agent_groups:
        plugins_for_group = groups_config.get(group, [])
        assigned_plugins.update(plugins_for_group)

    # Reject if the requested plugin is not assigned to this agent
    if name not in assigned_plugins:
        return jsonify({"error": "plugin not assigned to this agent"}), 403

    # Look up the agent section in the agents.toml file
    agent_config = agents_config.get(agentid, {})

    # Within the agent section: get the configuration for the plugin (plugin name equals <name>)
    plugin_config = agent_config.get(name, {})

    # Return the configuration as a dictionary
    return jsonify(plugin_config), 200
