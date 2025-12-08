from core import app, logger, SessionLocal
from flask import request, jsonify
from datetime import datetime
from dateutil import parser as dateutil_parser
from auth import require_agent_apikey
from db_models import Metrics, Alarm
import toml
import base64


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """
    Parse an ISO 8601 timestamp string into a datetime object.
    Returns None if value is None or empty.
    Raises ValueError if parsing fails.
    """
    if not value:
        return None
    return dateutil_parser.isoparse(value)


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


@app.route(
    "/agents/<agentname>/plugins/<pluginname>/metrics/<metricname>",
    methods=["GET"],
)
@require_agent_apikey
def list_agent_plugin_metric_data(agentname: str, pluginname: str, metricname: str):
    """
    List all metric data points for the given agent, plugin and metric.
    Supports optional 'from' and 'to' query parameters to filter by timestamp.
    """
    try:
        # metricname is expected to be base64-encoded UTF-8
        decoded_metricname = base64.b64decode(metricname).decode("utf-8")
    except Exception as e:
        logger.warning("Invalid base64 metricname '%s': %s", metricname, e)
        return jsonify({"error": "Invalid base64-encoded metricname"}), 400

    from_param = request.args.get("from")
    to_param = request.args.get("to")

    try:
        ts_from = _parse_iso_timestamp(from_param)
        ts_to = _parse_iso_timestamp(to_param)
    except ValueError as e:
        logger.warning("Invalid timestamp in query params: %s", e)
        return jsonify({"error": "Invalid 'from' or 'to' timestamp format"}), 400

    session = SessionLocal()
    try:
        query = (
            session.query(Metrics, Alarm)
            .outerjoin(Alarm, Metrics.id == Alarm.metrics_id)
            .filter(
                Metrics.agentid == agentname,
                Metrics.pluginid == pluginname,
                Metrics.metric == decoded_metricname,
            )
        )

        if ts_from is not None:
            query = query.filter(Metrics.timestamp >= ts_from)
        if ts_to is not None:
            query = query.filter(Metrics.timestamp <= ts_to)

        query = query.order_by(Metrics.timestamp.asc())
        rows = query.all()

        result = []
        for metric_row, alarm_row in rows:
            if metric_row.value_float is not None:
                value = metric_row.value_float
            elif metric_row.value_int is not None:
                value = metric_row.value_int
            else:
                value = metric_row.value_str

            data_point = {
                "timestamp": metric_row.timestamp.isoformat(),
                "value": value,
            }

            if alarm_row:
                data_point["alarm_id"] = alarm_row.id
                data_point["acknowledged"] = alarm_row.acknowledged

            result.append(data_point)

        return jsonify(result), 200
    except Exception as e:
        logger.error(
            "Error querying metric data for agent '%s', plugin '%s', metric '%s': %s",
            agentname,
            pluginname,
            decoded_metricname,
            e,
        )
        return jsonify({"error": "Error while querying metrics"}), 500
    finally:
        session.close()
