from datetime import UTC, datetime

from flask import jsonify, request
from sqlalchemy import and_

from db_models import Metrics
from functions import _parse_time_param, dict_value_to_metric, get_value_from_row
from rules import evaluate_rules_for_payload
from core import SessionLocal, app, logger


def _query_metrics(
    session,
    agentid: str | None = None,
    pluginid: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    search: str | None = None,
) -> list[dict]:
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
        result.append(
            {
                "agentid": row.agentid,
                "pluginid": row.pluginid,
                "metric": row.metric,
                "timestamp": row.timestamp.isoformat(),
                "value": get_value_from_row(row),
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


@app.route("/metrics", methods=["POST"])
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

    logger.info(f"Received data from agentid: {agentid}")
    logger.debug("Received payload: %s", payload)

    # Open a SQLAlchemy session
    session = SessionLocal()

    if not isinstance(payload, dict):
        raise ValueError("invalid payload: only dict is allowed.")
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
            logger.error("invalid timestamp format: %s", e)
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
