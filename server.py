import logging
import os
from datetime import datetime, UTC

import toml
from flask import Flask, jsonify, request
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

# SQLAlchemy ORM Setup
DATABASE_URL = "sqlite:///metrics.db"
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# Erstelle alle Tabellen, falls sie noch nicht existieren
Base.metadata.create_all(bind=engine)


@app.route("/status", methods=["GET"])
def status():
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
    agentid = request.headers.get("agentid", None)

    # Lade Konfiguration aus config.toml
    try:
        config = toml.load("conf/config.toml")
    except Exception as e:
        return jsonify({"error": f"Fehler beim Laden der Konfiguration: {e!s}"}), 500

    # Ermittle die Gruppen des Agenten (falls definiert)
    agent_groups = config.get("agents", {}).get(agentid, [])

    # Sammle alle Plugins, die den Gruppen des Agenten zugeordnet sind
    assigned_plugins = set()
    groups_config = config.get("groups", {})
    for group in agent_groups:
        plugins_for_group = groups_config.get(group, [])
        assigned_plugins.update(plugins_for_group)

    # Falls keine Gruppen/Plugins definiert sind, kann ein leerer Satz oder z. B. alle Plugins zurückgegeben werden.
    # Hier: Rückgabe der gefilterten Plugins gemäß der Konfiguration
    return jsonify(list(assigned_plugins)), 200


@app.route("/plugin/<name>", methods=["GET"])
def get_plugin(name):
    # Erstelle den Pfad zur Python-Skriptdatei im Ordner 'plugins'
    plugin_path = os.path.join("plugins", f"{name}.py")

    # Überprüfe, ob die Datei existiert und lesbar ist
    if not os.path.exists(plugin_path):
        return jsonify({"error": f"Plugin '{name}' nicht gefunden."}), 404

    try:
        with open(plugin_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return jsonify({"error": f"Fehler beim Lesen des Plugins: {e!s}"}), 500

    # Rückgabe des Inhalts als reinen Text
    return content, 200, {"Content-Type": "text/plain"}


@app.route("/plugin/<name>/config", methods=["GET"])
def get_plugin_config(name):
    # Hole die agentid aus dem HTTP-Header
    agentid = request.headers.get("agentid", None)
    if not agentid:
        return jsonify({"error": "agentid header missing"}), 400

    try:
        # Lade den Inhalt der agents.toml Datei
        config = toml.load("conf/agents.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der agents.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    # Suche in der TOML-Datei nach der Agentensektion
    agent_config = config.get(agentid, {})

    # Innerhalb der Agentensektion: Hole die Konfiguration für das Plugin (plugin name entspricht <name>)
    plugin_config = agent_config.get(name, {})

    # Retourniere die Konfiguration als Dictionary
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
        # case-insensitive LIKE-Suche auf der Spalte metric
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
    # Neuer /metric Endpoint: Payload und agentid-Header ausgeben
    agentid = request.headers.get("agentid", "Unknown")
    payload = request.get_json(silent=True)

    logger.info(f"AgentID: {agentid}")
    logger.debug("Received payload: %s", payload)

    # Öffne eine SQLAlchemy-Session
    session = SessionLocal()

    if not isinstance(payload, dict):
        raise ValueError("Ungültige Payload: Es wird ein Dictionary erwartet")
    # Erwarte Payload-Struktur: pluginid, agentid, timestamp, metrics (als Liste)
    pluginid = payload.get("pluginid")
    if not pluginid:
        raise ValueError("no plugin id found.")
    # Bevorzugt den agentid-Wert aus Header, ansonsten aus der Payload
    agentid_payload = payload.get("agentid", agentid)

    timestamp = payload.get("timestamp")
    # Umwandeln des Timestamps, falls er ein String ist (ISO 8601 Format erwartet)
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError as e:
            logger.error("Ungültiges Timestamp-Format: %s", e)
            timestamp = datetime.now(UTC)
    elif not isinstance(timestamp, datetime):
        # Falls kein gültiger Timestamp übermittelt wurde, verwende den aktuellen Zeitpunkt
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
