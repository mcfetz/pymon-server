from core import app, logger, SessionLocal
from flask import request, jsonify
from auth import require_agent_apikey
from db_models import Metrics
import toml


@app.route("/agents/status", methods=["GET"])
@require_agent_apikey
def status():
    """
    Get agent status.
    ---
    tags:
      - status
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
      - in: query
        name: online
        required: false
        schema:
          type: string
        description: If present, status will be set to "online"
      - in: query
        name: offline
        required: false
        schema:
          type: string
        description: If present, status will be set to "offline"
    responses:
      200:
        description: Current status of the agent
        content:
          text/plain:
            schema:
              type: string
      401:
        description: Invalid or missing API key
    """
    agentid = request.agentid
    status = None
    if "online" in request.args:
        status = "online"
    elif "offline" in request.args:
        status = "offline"
    else:
        status = "undefined"

    logger.info(f"AgentID: {agentid}, Status: {status}")
    return f"AgentID: {agentid}, Status: {status}", 200


@app.route("/agents", methods=["GET"])
@require_agent_apikey
def list_agents():
    """
    List all known agents from agents.toml.
    ---
    tags:
      - agents
    responses:
      200:
        description: List of agent ids
        content:
          application/json:
            schema:
              type: array
              items:
                type: string
      500:
        description: Error while loading agents configuration
    """
    try:
        agents_config = toml.load("conf/agents.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der agents.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    # Top-level keys in agents.toml are agent ids
    agent_ids = list(agents_config.keys())
    return jsonify(agent_ids), 200


@app.route("/groups", methods=["GET"])
@require_agent_apikey
def list_groups():
    """
    List all known groups from config.toml.
    ---
    tags:
      - agents
    responses:
      200:
        description: List of groups and their assigned agents
        content:
          application/json:
            schema:
              type: object
              properties:
                groups:
                  type: array
                  items:
                    type: string
                agents:
                  type: object
                  additionalProperties:
                    type: array
                    items:
                      type: string
      500:
        description: Error while loading configuration
    """
    try:
        config = toml.load("conf/config.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der config.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    groups_section = config.get("groups", {})
    group_names = list(groups_section.keys())

    # Build mapping: group -> list of agents assigned to that group
    agents_section = config.get("agents", {})
    group_to_agents: dict[str, list[str]] = {group: [] for group in group_names}

    for agent_id, agent_groups in agents_section.items():
        for group in agent_groups:
            if group in group_to_agents:
                group_to_agents[group].append(agent_id)

    return jsonify(group_to_agents), 200


@app.route("/agents/<agentname>/plugins", methods=["GET"])
@require_agent_apikey
def list_agent_plugins(agentname: str):
    """
    List all plugins assigned to a given agent via groups in config.toml.
    ---
    tags:
      - agents
    parameters:
      - in: path
        name: agentname
        required: true
        schema:
          type: string
        description: Agent identifier
    responses:
      200:
        description: List of plugin names assigned to the agent
        content:
          application/json:
            schema:
              type: array
              items:
                type: string
      404:
        description: Agent not found in configuration
      500:
        description: Error while loading configuration
    """
    try:
        config = toml.load("conf/config.toml")
    except Exception as e:
        logger.error("Error loading config.toml: %s", e)
        return jsonify({"error": "Error loading configuration"}), 500

    agents_section = config.get("agents", {})
    groups_section = config.get("groups", {})

    # Get groups assigned to this agent
    agent_groups = agents_section.get(agentname)
    if agent_groups is None:
        return jsonify({"error": "Agent not found"}), 404

    # Collect plugins from all groups assigned to the agent
    plugins: set[str] = set()
    for group in agent_groups:
        group_plugins = groups_section.get(group, [])
        for plugin in group_plugins:
            plugins.add(plugin)

    return jsonify(sorted(plugins)), 200


@app.route("/agents/<agentname>/plugins/<pluginname>/metrics", methods=["GET"])
@require_agent_apikey
def list_agent_plugin_metrics(agentname: str, pluginname: str):
    """
    List all unique metric names stored in the database for the given agent and plugin.
    ---
    tags:
      - agents
    parameters:
      - in: path
        name: agentname
        required: true
        schema:
          type: string
        description: Agent identifier
      - in: path
        name: pluginname
        required: true
        schema:
          type: string
        description: Plugin identifier
    responses:
      200:
        description: List of unique metric names for the given agent and plugin
        content:
          application/json:
            schema:
              type: array
              items:
                type: string
      500:
        description: Error while querying metrics
    """
    session = SessionLocal()
    try:
        # Query distinct metric names for this agent + plugin
        rows = (
            session.query(Metrics.metric)
            .filter(
                Metrics.agentid == agentname,
                Metrics.pluginid == pluginname,
            )
            .distinct()
            .all()
        )

        # rows is a list of 1-tuples; extract and sort unique metric names
        metric_names = sorted({row[0] for row in rows if row[0] is not None})

        return jsonify(metric_names), 200
    except Exception as e:
        logger.error(
            "Error querying metrics for agent '%s', plugin '%s': %s",
            agentname,
            pluginname,
            e,
        )
        return jsonify({"error": "Error while querying metrics"}), 500
    finally:
        session.close()
