import logging
import os
from datetime import datetime, UTC

import toml
from flask import Flask, jsonify, request
from flasgger import Swagger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db_models import Base, Metrics, Alarm
from rules import evaluate_rules_for_payload
from functions import dict_value_to_metric

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)
app = Flask(__name__)
swagger = Swagger(app)

# SQLAlchemy ORM Setup
DATABASE_URL = "sqlite:///metrics.db"
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Create all tables if they do not exist yet
Base.metadata.create_all(bind=engine)


@app.route("/status", methods=["GET"])
def status():
    """
    Get agent status.
    ---
    tags:
      - status
    parameters:
      - in: header
        name: agentid
        required: false
        schema:
          type: string
        description: Agent identifier
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
    """
    agentid = request.headers.get("agentid", "Unknown")
    status = None
    if "online" in request.args:
        status = "online"
    elif "offline" in request.args:
        status = "offline"
    else:
        status = "undefined"

    logger.info(f"AgentID: {agentid}, Status: {status}")
    return f"AgentID: {agentid}, Status: {status}", 200


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


@app.route("/plugin/<name>", methods=["GET"])
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


@app.route("/plugin/<name>/config", methods=["GET"])
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
        config = toml.load("conf/agents.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der agents.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    # Look up the agent section in the TOML file
    agent_config = config.get(agentid, {})

    # Within the agent section: get the configuration for the plugin (plugin name equals <name>)
    plugin_config = agent_config.get(name, {})

    # Return the configuration as a dictionary
    return jsonify(plugin_config), 200


def _parse_time_param(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _query_metrics(
    session,
    agentid: str | None = None,
    pluginid: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    search: str | None = None,
) -> list[dict]:
    from sqlalchemy import and_

    q = session.query(Metrics)

    filters = []
    if agentid is not None:
        filters.append(Metrics.agentid == agentid)
    if pluginid is not None:
        filters.append(Metrics.pluginid == pluginid)
    if time_from is not None:
        filters.append(Metrics.timestamp >= time_from)
    if time_to is not None:
        filters.append(Metrics.timestamp <= time_to)
    if search is not None:
        # case-insensitive LIKE search on the metric column
        filters.append(Metrics.metric.ilike(f"%{search}%"))

    if filters:
        q = q.filter(and_(*filters))

    rows = q.order_by(Metrics.timestamp.asc()).all()

    result: list[dict] = []
    for row in rows:
        # Wert aus value_float / value_int bestimmen
        value = row.value_float if row.value_float is not None else row.value_int
        result.append(
            {
                "agentid": row.agentid,
                "pluginid": row.pluginid,
                "metric": row.metric,
                "timestamp": row.timestamp.isoformat(),
                "value": value,
            }
        )
    return result


@app.route("/metrics", methods=["GET"])
def get_metrics_all():
    """
    Query metrics for all agents and plugins.
    ---
    tags:
      - metrics
    parameters:
      - in: query
        name: time-from
        required: false
        schema:
          type: string
          format: date-time
        description: Start timestamp (ISO 8601)
      - in: query
        name: time-to
        required: false
        schema:
          type: string
          format: date-time
        description: End timestamp (ISO 8601)
      - in: query
        name: search
        required: false
        schema:
          type: string
        description: Case-insensitive substring filter on metric name
    responses:
      200:
        description: List of metrics
        content:
          application/json:
            schema:
              type: array
              items:
                type: object
                properties:
                  agentid:
                    type: string
                  pluginid:
                    type: string
                  metric:
                    type: string
                  timestamp:
                    type: string
                    format: date-time
                  value:
                    type: number
      400:
        description: Invalid time-from or time-to format
    """
    # optionale Query-Parameter: time-from, time-to (ISO 8601)
    time_from_param = request.args.get("time-from")
    time_to_param = request.args.get("time-to")
    search = request.args.get("search")

    time_from = _parse_time_param(time_from_param)
    time_to = _parse_time_param(time_to_param)

    if (time_from_param and time_from is None) or (time_to_param and time_to is None):
        return jsonify({"error": "invalid time-from or time-to format, expected ISO 8601"}), 400

    session = SessionLocal()
    try:
        data = _query_metrics(
            session,
            agentid=None,
            pluginid=None,
            time_from=time_from,
            time_to=time_to,
            search=search,
        )
        return jsonify(data), 200
    finally:
        session.close()


@app.route("/metrics/<agentid>", methods=["GET"])
def get_metrics_for_agent(agentid: str):
    """
    Query metrics for a specific agent.
    ---
    tags:
      - metrics
    parameters:
      - in: path
        name: agentid
        required: true
        schema:
          type: string
        description: Agent identifier
      - in: query
        name: time-from
        required: false
        schema:
          type: string
          format: date-time
        description: Start timestamp (ISO 8601)
      - in: query
        name: time-to
        required: false
        schema:
          type: string
          format: date-time
        description: End timestamp (ISO 8601)
      - in: query
        name: search
        required: false
        schema:
          type: string
        description: Case-insensitive substring filter on metric name
    responses:
      200:
        description: List of metrics for the agent
        content:
          application/json:
            schema:
              type: array
              items:
                type: object
                properties:
                  agentid:
                    type: string
                  pluginid:
                    type: string
                  metric:
                    type: string
                  timestamp:
                    type: string
                    format: date-time
                  value:
                    type: number
      400:
        description: Invalid time-from or time-to format
    """
    time_from_param = request.args.get("time-from")
    time_to_param = request.args.get("time-to")
    search = request.args.get("search")

    time_from = _parse_time_param(time_from_param)
    time_to = _parse_time_param(time_to_param)

    if (time_from_param and time_from is None) or (time_to_param and time_to is None):
        return jsonify({"error": "invalid time-from or time-to format, expected ISO 8601"}), 400

    session = SessionLocal()
    try:
        data = _query_metrics(
            session,
            agentid=agentid,
            pluginid=None,
            time_from=time_from,
            time_to=time_to,
            search=search,
        )
        return jsonify(data), 200
    finally:
        session.close()


@app.route("/metrics/<agentid>/<pluginid>", methods=["GET"])
def get_metrics_for_agent_plugin(agentid: str, pluginid: str):
    """
    Query metrics for a specific agent and plugin.
    ---
    tags:
      - metrics
    parameters:
      - in: path
        name: agentid
        required: true
        schema:
          type: string
        description: Agent identifier
      - in: path
        name: pluginid
        required: true
        schema:
          type: string
        description: Plugin identifier
      - in: query
        name: time-from
        required: false
        schema:
          type: string
          format: date-time
        description: Start timestamp (ISO 8601)
      - in: query
        name: time-to
        required: false
        schema:
          type: string
          format: date-time
        description: End timestamp (ISO 8601)
      - in: query
        name: search
        required: false
        schema:
          type: string
        description: Case-insensitive substring filter on metric name
    responses:
      200:
        description: List of metrics for the agent and plugin
        content:
          application/json:
            schema:
              type: array
              items:
                type: object
                properties:
                  agentid:
                    type: string
                  pluginid:
                    type: string
                  metric:
                    type: string
                  timestamp:
                    type: string
                    format: date-time
                  value:
                    type: number
      400:
        description: Invalid time-from or time-to format
    """
    time_from_param = request.args.get("time-from")
    time_to_param = request.args.get("time-to")
    search = request.args.get("search")

    time_from = _parse_time_param(time_from_param)
    time_to = _parse_time_param(time_to_param)

    if (time_from_param and time_from is None) or (time_to_param and time_to is None):
        return jsonify({"error": "invalid time-from or time-to format, expected ISO 8601"}), 400

    session = SessionLocal()
    try:
        data = _query_metrics(
            session,
            agentid=agentid,
            pluginid=pluginid,
            time_from=time_from,
            time_to=time_to,
            search=search,
        )
        return jsonify(data), 200
    finally:
        session.close()


@app.route("/metric", methods=["POST"])
def collect_metrics():
    """
    Ingest metrics from an agent.
    ---
    tags:
      - metrics
    parameters:
      - in: header
        name: agentid
        required: false
        schema:
          type: string
        description: Agent identifier (overrides payload agentid if present)
      - in: body
        name: payload
        required: true
        schema:
          type: object
          properties:
            pluginid:
              type: string
            agentid:
              type: string
            timestamp:
              type: string
              format: date-time
            metrics:
              type: array
              items:
                type: object
                additionalProperties:
                  oneOf:
                    - type: number
                    - type: integer
                    - type: boolean
                    - type: string
    responses:
      200:
        description: Metrics stored successfully
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
      400:
        description: Invalid payload
      500:
        description: Internal error while storing or evaluating metrics
    """
    # /metric endpoint: store payload and agentid header
    agentid = request.headers.get("agentid", "Unknown")
    payload = request.get_json(silent=True)

    logger.info(f"AgentID: {agentid}")
    logger.debug("Received payload: %s", payload)

    # Open a SQLAlchemy session
    session = SessionLocal()

    if not isinstance(payload, dict):
        raise ValueError("Ungültige Payload: Es wird ein Dictionary erwartet")
    # Expect payload structure: pluginid, agentid, timestamp, metrics (as list)
    pluginid = payload.get("pluginid")
    if not pluginid:
        raise ValueError("no plugin id found.")
    # Prefer agentid value from header, otherwise from payload
    agentid_payload = payload.get("agentid", agentid)

    timestamp = payload.get("timestamp")
    # Convert timestamp if it is a string (expects ISO 8601 format)
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError as e:
            logger.error("Ungültiges Timestamp-Format: %s", e)
            timestamp = datetime.now(UTC)
    elif not isinstance(timestamp, datetime):
        # If no valid timestamp was provided, use the current time
        timestamp = datetime.now(UTC)

    metrics_list = payload.get("metrics", [])

    for metric_dict in metrics_list:
        if isinstance(metric_dict, dict):
            for metric_name, value in metric_dict.items():
                metric_entry = Metrics(
                    agentid=agentid_payload,
                    pluginid=pluginid,
                    timestamp=timestamp,
                    metric=metric_name,
                )
                metric_entry = dict_value_to_metric(value, metric_entry)
                session.add(metric_entry)

    session.commit()

    # Evaluate rules for the received metrics
    try:
        evaluate_rules_for_payload(session, agentid_payload, pluginid, metrics_list)
        session.commit()
    except Exception as e:
        logger.error("Error while evaluating rules: %s", e)
        session.rollback()

    session.close()

    return jsonify({"status": "Metrics stored"}), 200


@app.route("/alarms/<int:alarmid>/ack", methods=["GET"])
def acknowledge_alarm(alarmid: int):
    """
    Acknowledge an alarm.
    ---
    tags:
      - alarms
    parameters:
      - in: path
        name: alarmid
        required: true
        schema:
          type: integer
        description: Alarm id
    responses:
      200:
        description: Alarm acknowledged
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
                alarmid:
                  type: integer
      404:
        description: Alarm not found
      500:
        description: Internal error while acknowledging alarm
    """
    session = SessionLocal()
    try:
        alarm = session.get(Alarm, alarmid)
        if alarm is None:
            session.close()
            return jsonify({"error": f"Alarm with id {alarmid} not found"}), 404

        alarm.acknowledged = True
        session.commit()
        session.close()
        return jsonify({"status": "acknowledged", "alarmid": alarmid}), 200
    except Exception as e:
        session.rollback()
        session.close()
        logger.error("Error while acknowledging alarm %s: %s", alarmid, e)
        return jsonify({"error": "internal error"}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
