from flask import jsonify, request
from core import app, logger, SessionLocal
from db_models import Alarm
from auth import require_agent_apikey
from sqlalchemy import desc


@app.route("/alarms", methods=["GET"])
@require_agent_apikey
def list_alarms():
    """
    List alarms with optional filters.
    ---
    tags:
      - alarms
    parameters:
      - in: query
        name: acknowledged
        required: false
        schema:
          type: boolean
        description: Filter by acknowledged status
      - in: query
        name: agentid
        required: false
        schema:
          type: string
        description: Filter by agent
      - in: query
        name: limit
        required: false
        schema:
          type: integer
        description: Max results (default 100)
    responses:
      200:
        description: List of alarms
        content:
          application/json:
            schema:
              type: array
              items:
                type: object
    """
    session = SessionLocal()
    try:
        q = session.query(Alarm)

        ack_param = request.args.get("acknowledged")
        if ack_param is not None:
            if ack_param.lower() in ("true", "1"):
                q = q.filter(Alarm.acknowledged == True)  # noqa: E712
            elif ack_param.lower() in ("false", "0"):
                q = q.filter(Alarm.acknowledged == False)  # noqa: E712

        agentid_param = request.args.get("agentid")
        if agentid_param:
            q = q.filter(Alarm.agentid == agentid_param)

        limit = request.args.get("limit", 100, type=int)
        q = q.order_by(desc(Alarm.created_at)).limit(limit)

        alarms = q.all()
        result = []
        for a in alarms:
            result.append({
                "id": a.id,
                "agentid": a.agentid,
                "rule_id": a.rule_id,
                "pluginid": a.pluginid,
                "metric": a.metric,
                "severity": a.severity,
                "value": a.value,
                "created_at": a.created_at.isoformat(),
                "message": a.message,
                "acknowledged": a.acknowledged,
                "metrics_id": a.metrics_id,
            })
        return jsonify(result), 200
    except Exception as e:
        logger.error("Error listing alarms: %s", e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()


@app.route("/alarms/open", methods=["GET"])
@require_agent_apikey
def list_open_alarms():
    """List all open (unacknowledged) alarms."""
    session = SessionLocal()
    try:
        q = session.query(Alarm).filter(Alarm.acknowledged == False).order_by(desc(Alarm.created_at)).limit(100)  # noqa: E712
        alarms = q.all()
        result = []
        for a in alarms:
            result.append({
                "id": a.id,
                "agentid": a.agentid,
                "rule_id": a.rule_id,
                "pluginid": a.pluginid,
                "metric": a.metric,
                "severity": a.severity,
                "value": a.value,
                "created_at": a.created_at.isoformat(),
                "message": a.message,
                "acknowledged": a.acknowledged,
                "metrics_id": a.metrics_id,
            })
        return jsonify(result), 200
    except Exception as e:
        logger.error("Error listing open alarms: %s", e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()


@app.route("/alarms/<int:alarmid>/ack", methods=["GET"])
@require_agent_apikey
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
