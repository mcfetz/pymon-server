import json
import os
from datetime import datetime, UTC

from flask import jsonify, request
from core import app, logger, SessionLocal
from db_models import Alarm, Metrics
from auth import require_agent_apikey
from sqlalchemy import desc, asc
from config import CONF_DIR

SNOOZE_FILE = os.path.join(CONF_DIR, "snoozes.json")


def _alarm_to_dict(a: Alarm) -> dict:
    return {
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
    }


def _load_snoozes() -> list[dict]:
    try:
        with open(SNOOZE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_snoozes(snoozes: list[dict]) -> None:
    with open(SNOOZE_FILE, "w", encoding="utf-8") as f:
        json.dump(snoozes, f, indent=2)


def _snooze_key(rule_id: str, agentid: str, pluginid: str, metric: str) -> str:
    return f"{rule_id}|{agentid}|{pluginid}|{metric}"


def is_snoozed(rule_id: str, agentid: str, pluginid: str, metric: str) -> bool:
    key = _snooze_key(rule_id, agentid, pluginid, metric)
    return any(_snooze_key(s["rule_id"], s["agentid"], s["pluginid"], s["metric"]) == key for s in _load_snoozes())


def clear_snooze_for_alarm(rule_id: str, agentid: str, pluginid: str, metric: str) -> None:
    """Remove snooze for a combo (used when all alarms for it are acknowledged)."""
    key = _snooze_key(rule_id, agentid, pluginid, metric)
    snoozes = _load_snoozes()
    snoozes = [s for s in snoozes if _snooze_key(s["rule_id"], s["agentid"], s["pluginid"], s["metric"]) != key]
    _save_snoozes(snoozes)


@app.route("/alarms/snoozed", methods=["GET"])
@require_agent_apikey
def list_snoozed():
    """List snoozes, auto-clearing stale entries where no open alarms remain."""
    from core import SessionLocal
    from db_models import Alarm

    snoozes = _load_snoozes()
    if not snoozes:
        return jsonify([]), 200

    session = SessionLocal()
    try:
        clean = []
        dirty = False
        for s in snoozes:
            remaining = session.query(Alarm).filter(
                Alarm.rule_id == s["rule_id"],
                Alarm.agentid == s["agentid"],
                Alarm.pluginid == s["pluginid"],
                Alarm.metric == s["metric"],
                Alarm.acknowledged == False,
            ).count()
            if remaining > 0:
                clean.append(s)
            else:
                dirty = True
        if dirty:
            _save_snoozes(clean)
        return jsonify(clean), 200
    finally:
        session.close()


@app.route("/alarms/snooze/toggle", methods=["POST"])
@require_agent_apikey
def toggle_snooze():
    data = request.get_json(silent=True) or {}
    rule_id = data.get("rule_id")
    agentid = data.get("agentid")
    pluginid = data.get("pluginid")
    metric = data.get("metric")
    if not all([rule_id, agentid, pluginid, metric]):
        return jsonify({"error": "rule_id, agentid, pluginid, metric required"}), 400

    key = _snooze_key(rule_id, agentid, pluginid, metric)
    snoozes = _load_snoozes()
    for i, s in enumerate(snoozes):
        if _snooze_key(s["rule_id"], s["agentid"], s["pluginid"], s["metric"]) == key:
            snoozes.pop(i)
            _save_snoozes(snoozes)
            return jsonify({"status": "unsnoozed"}), 200

    snoozes.append({
        "rule_id": rule_id,
        "agentid": agentid,
        "pluginid": pluginid,
        "metric": metric,
        "snoozed_at": datetime.now(UTC).isoformat(),
    })
    _save_snoozes(snoozes)
    return jsonify({"status": "snoozed"}), 200


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

        limit = request.args.get("limit", 500, type=int)
        q = q.order_by(desc(Alarm.created_at)).limit(limit + 1)

        alarms = q.all()
        truncated = len(alarms) > limit
        if truncated:
            alarms = alarms[:limit]

        result = []
        for a in alarms:
            result.append(_alarm_to_dict(a))
        resp = jsonify(result)
        if truncated:
            resp.headers["X-Truncated"] = "true"
        return resp, 200
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
        q = session.query(Alarm).filter(Alarm.acknowledged == False).order_by(desc(Alarm.created_at)).limit(501)  # noqa: E712
        alarms = q.all()
        truncated = len(alarms) > 500
        if truncated:
            alarms = alarms[:500]
        result = [_alarm_to_dict(a) for a in alarms]
        resp = jsonify(result)
        if truncated:
            resp.headers["X-Truncated"] = "true"
        return resp, 200
    except Exception as e:
        logger.error("Error listing open alarms: %s", e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()


@app.route("/alarms/<int:alarmid>", methods=["GET"])
@require_agent_apikey
def get_alarm_detail(alarmid: int):
    """Get a single alarm with full context: surrounding alarms, rule info, agent info, metric history."""
    session = SessionLocal()
    try:
        alarm = session.get(Alarm, alarmid)
        if alarm is None:
            return jsonify({"error": "not found"}), 404

        result = _alarm_to_dict(alarm)

        # 5 alarms before and after (same rule+agent+plugin+metric), ordered by id
        before = session.query(Alarm).filter(
            Alarm.rule_id == alarm.rule_id,
            Alarm.agentid == alarm.agentid,
            Alarm.pluginid == alarm.pluginid,
            Alarm.metric == alarm.metric,
            Alarm.id < alarmid,
        ).order_by(desc(Alarm.id)).limit(5).all()

        after = session.query(Alarm).filter(
            Alarm.rule_id == alarm.rule_id,
            Alarm.agentid == alarm.agentid,
            Alarm.pluginid == alarm.pluginid,
            Alarm.metric == alarm.metric,
            Alarm.id > alarmid,
        ).order_by(asc(Alarm.id)).limit(5).all()

        result["surrounding"] = sorted(
            [_alarm_to_dict(a) for a in before + after],
            key=lambda x: x["id"],
        )
        result["total_same_type"] = session.query(Alarm).filter(
            Alarm.rule_id == alarm.rule_id,
            Alarm.agentid == alarm.agentid,
            Alarm.pluginid == alarm.pluginid,
            Alarm.metric == alarm.metric,
        ).count()

        # Rule info from rules.json
        try:
            with open(os.path.join(CONF_DIR, "rules.json"), encoding="utf-8") as f:
                result["rule"] = json.load(f).get(alarm.rule_id)
        except Exception:
            result["rule"] = None

        # Agent info from agents.json
        try:
            with open(os.path.join(CONF_DIR, "agents.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            agent = cfg.get("agents", {}).get(alarm.agentid, {})
            result["agent"] = {
                "title": agent.get("title", alarm.agentid),
                "description": agent.get("description", ""),
                "enabled": agent.get("enabled", True),
            }
        except Exception:
            result["agent"] = {"title": alarm.agentid, "description": "", "enabled": True}

        # 30 most recent metric values for sparkline (reverse-chronological fetch, then reverse)
        metrics_rows = session.query(Metrics).filter(
            Metrics.agentid == alarm.agentid,
            Metrics.pluginid == alarm.pluginid,
            Metrics.metric == alarm.metric,
        ).order_by(desc(Metrics.timestamp)).limit(30).all()

        result["metric_history"] = [
            {
                "timestamp": m.timestamp.isoformat(),
                "value": m.value_float if m.value_float is not None else m.value_int,
            }
            for m in reversed(metrics_rows)
            if m.value_float is not None or m.value_int is not None
        ]

        return jsonify(result), 200
    except Exception as e:
        logger.error("Error fetching alarm detail %s: %s", alarmid, e)
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
            return jsonify({"error": f"Alarm with id {alarmid} not found"}), 404

        alarm.acknowledged = True
        session.flush()

        # Auto-unsnooze when no more open alarms for this combo
        remaining = session.query(Alarm).filter(
            Alarm.rule_id == alarm.rule_id,
            Alarm.agentid == alarm.agentid,
            Alarm.pluginid == alarm.pluginid,
            Alarm.metric == alarm.metric,
            Alarm.acknowledged == False,  # noqa: E712
        ).count()
        if remaining == 0:
            clear_snooze_for_alarm(alarm.rule_id, alarm.agentid, alarm.pluginid, alarm.metric)

        session.commit()
        return jsonify({"status": "acknowledged", "alarmid": alarmid}), 200
    except Exception as e:
        session.rollback()
        logger.error("Error while acknowledging alarm %s: %s", alarmid, e)
        return jsonify({"error": "internal error"}), 500
    finally:
        session.close()
