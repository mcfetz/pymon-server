from datetime import UTC, datetime
import json
import os

from flask import jsonify, request
from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from db_models import Alarm, Metrics
from functions import _parse_time_param, dict_value_to_metric, get_value_from_row
from rules import evaluate_rules_for_payload
from core import SessionLocal, app, logger
from auth import require_agent_apikey


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
@require_agent_apikey
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
@require_agent_apikey
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
@require_agent_apikey
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
@require_agent_apikey
def collect_metrics():
    """
    Ingest metrics from an agent.
    ---
    tags:
      - metrics
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
              description: Ignored; authenticated agentid from header is used
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
      401:
        description: Invalid or missing API key
      500:
        description: Internal error while storing or evaluating metrics
    """
    # /metrics endpoint: store payload and authenticated agentid
    agentid = request.agentid
    payload = request.get_json(silent=True)

    logger.info(f"Received data from agentid: {agentid}")
    logger.debug("Received payload: %s", payload)

    # Check if agent is enabled
    from routes.admin import _load_json_config
    cfg = _load_json_config()
    agent_cfg = cfg.get("agents", {}).get(agentid)
    if agent_cfg is not None and not agent_cfg.get("enabled", True):
        logger.info("Agent %s is disabled, discarding metrics", agentid)
        return jsonify({"status": "agent disabled, metrics discarded"}), 200

    # Open a SQLAlchemy session
    session = SessionLocal()
    try:
        if not isinstance(payload, dict):
            return jsonify({"error": "invalid payload: only dict is allowed."}), 400
        # Expect payload structure: pluginid, agentid, timestamp, metrics (as list)
        pluginid = payload.get("pluginid")
        if not pluginid:
            return jsonify({"error": "no plugin id found."}), 400
        # Always use authenticated agentid from header
        agentid_payload = agentid

        timestamp = payload.get("timestamp")
        # Convert timestamp: accept ISO 8601 string, Unix float, or datetime
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError as e:
                logger.error("invalid timestamp format: %s", e)
                timestamp = datetime.now(UTC)
        elif isinstance(timestamp, (int, float)):
            # Unix timestamp (seconds since epoch) from agent
            timestamp = datetime.fromtimestamp(timestamp, tz=UTC)
        elif not isinstance(timestamp, datetime):
            # If no valid timestamp was provided, use the current time
            timestamp = datetime.now(UTC)

        metrics_list = payload.get("metrics", [])
        db_metrics = []

        for metric_dict in metrics_list:
            if isinstance(metric_dict, dict):
                for metric_name, value in metric_dict.items():
                    try:
                        metric_entry = Metrics(
                            agentid=agentid_payload,
                            pluginid=pluginid,
                            timestamp=timestamp,
                            metric=metric_name,
                        )
                        metric_entry = dict_value_to_metric(value, metric_entry)
                        session.add(metric_entry)
                        db_metrics.append(metric_entry)
                    except Exception as e:
                        logger.error("Error storing metric '%s' from plugin '%s': value=%r, error=%s", metric_name, pluginid, value, e)
                        raise

        # Flush to get IDs for new metrics, then evaluate rules.
        # This is all one transaction.
        session.flush()

        evaluate_rules_for_payload(session, agentid_payload, pluginid, db_metrics)
        session.commit()
    except Exception as e:
        logger.error("Error while storing metrics or evaluating rules: %s", e)
        session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

    return jsonify({"status": "Metrics stored"}), 200


def _resolve_group_agents(group_name: str) -> list[str]:
    """Resolve a group name to agent IDs using agents.json."""
    import json, os
    fpath = os.path.join(os.path.dirname(__file__), "..", "conf", "agents.json")
    try:
        with open(fpath) as f:
            cfg = json.load(f)
    except Exception:
        return []
    agents = cfg.get("agents", {})
    result = []
    for agent_id, agent_data in agents.items():
        if group_name in agent_data.get("groups", []):
            result.append(agent_id)
    return result


@app.route("/metrics/query", methods=["GET"])
@require_agent_apikey
def query_metrics():
    """
    Query metrics with filters and joined alarm data.
    ---
    tags:
      - metrics
    parameters:
      - in: query
        name: group
        required: false
        schema:
          type: string
        description: Filter by group name (resolves to agents)
      - in: query
        name: agentid
        required: false
        schema:
          type: string
        description: Filter by agent
      - in: query
        name: pluginid
        required: false
        schema:
          type: string
        description: Filter by plugin
      - in: query
        name: metric
        required: false
        schema:
          type: string
        description: Filter by metric name (case-insensitive LIKE)
      - in: query
        name: from
        required: false
        schema:
          type: string
          format: date-time
        description: Start timestamp (ISO 8601)
      - in: query
        name: to
        required: false
        schema:
          type: string
          format: date-time
        description: End timestamp (ISO 8601)
      - in: query
        name: limit
        required: false
        schema:
          type: integer
        description: Max results (default 200)
    responses:
      200:
        description: List of metrics with optional alarm info
    """
    group_param = request.args.get("group")
    agentid_raw = request.args.get("agentid")
    pluginid_param = request.args.get("pluginid")
    metric_param = request.args.get("metric")
    time_from_param = request.args.get("from")
    time_to_param = request.args.get("to")
    limit = request.args.get("limit", 200, type=int)
    offset = request.args.get("offset", 0, type=int)

    time_from = _parse_time_param(time_from_param)
    time_to = _parse_time_param(time_to_param)

    # Resolve group → agents
    agentids: list[str] | None = None
    if group_param:
        agentids = _resolve_group_agents(group_param)
        if not agentids:
            return jsonify([]), 200
    elif agentid_raw:
        agentids = [a.strip() for a in agentid_raw.split(",") if a.strip()]

    session = SessionLocal()
    try:
        q = session.query(Metrics)

        if agentids is not None:
            q = q.filter(Metrics.agentid.in_(agentids))
        if pluginid_param:
            q = q.filter(Metrics.pluginid == pluginid_param)
        if metric_param:
            q = q.filter(Metrics.metric.ilike(f"%{metric_param}%"))
        if time_from is not None:
            q = q.filter(Metrics.timestamp >= time_from)
        if time_to is not None:
            q = q.filter(Metrics.timestamp <= time_to)

        rows = q.order_by(Metrics.timestamp.desc()).offset(offset).limit(limit).all()

        # Batch-load alarm info to avoid JOIN duplication (1 metric → N alarms)
        metric_ids = [m.id for m in rows]
        alarm_rows = (
            session.query(Alarm.metrics_id, Alarm.id, Alarm.acknowledged)
            .filter(Alarm.metrics_id.in_(metric_ids))
            .all()
        )
        alarm_map: dict[int, tuple[int, bool]] = {}
        for am_id, a_id, a_ack in alarm_rows:
            # Only keep the first (latest) alarm per metric
            if am_id not in alarm_map:
                alarm_map[am_id] = (a_id, a_ack)

        result = []
        for metric_row in rows:
            alarm_id, acknowledged = alarm_map.get(metric_row.id, (None, None))
            result.append({
                "id": metric_row.id,
                "timestamp": metric_row.timestamp.isoformat(),
                "agentid": metric_row.agentid,
                "pluginid": metric_row.pluginid,
                "metric": metric_row.metric,
                "value": get_value_from_row(metric_row),
                "alarm_id": alarm_id,
                "acknowledged": acknowledged,
            })
        return jsonify(result), 200
    except Exception as e:
        logger.error("Error in metrics query: %s", e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()
