from core import app, logger
from flask import request, jsonify
from auth import require_agent_apikey
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
