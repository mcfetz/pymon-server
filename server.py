import logging
import os
from datetime import datetime, UTC

import toml
from flask import Flask, jsonify, request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db_models import Base, Metrics
from rules import evaluate_rules_for_payload

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
        config = toml.load("config.toml")
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
        config = toml.load("agents.toml")
    except Exception as e:
        logger.error("Fehler beim Laden der agents.toml: %s", e)
        return jsonify({"error": "Fehler beim Laden der Konfiguration"}), 500

    # Suche in der TOML-Datei nach der Agentensektion
    agent_config = config.get(agentid, {})

    # Innerhalb der Agentensektion: Hole die Konfiguration für das Plugin (plugin name entspricht <name>)
    plugin_config = agent_config.get(name, {})

    # Retourniere die Konfiguration als Dictionary
    return jsonify(plugin_config), 200


@app.route("/metric", methods=["POST"])
def collect_metrics():
    # Neuer /metric Endpoint: Payload und agentid-Header ausgeben
    agentid = request.headers.get("agentid", "Unknown")
    payload = request.get_json(silent=True)

    logger.info(f"AgentID: {agentid}")
    logger.info("Received payload: %s", payload)

    # Öffne eine SQLAlchemy-Session
    session = SessionLocal()

    if not isinstance(payload, dict):
        raise ValueError("Ungültige Payload: Es wird ein Dictionary erwartet")
    # Erwarte Payload-Struktur: pluginid, agentid, timestamp, metrics (als Liste)
    pluginid = payload.get("pluginid")
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
                value_float = value_int = value_str = None
                if isinstance(value, float):
                    value_float = value
                elif isinstance(value, int):
                    value_int = value
                elif isinstance(value, str):
                    value_str = value
                elif isinstance(value, bool):
                    value_int = 1 if value else 0
                elif value:
                    value_str = str(value)

                metric_entry = Metrics(
                    agentid=agentid_payload,
                    pluginid=pluginid,
                    timestamp=timestamp,
                    metric=metric_name,
                    value_float=value_float,
                    value_int=value_int,
                    value_str=value_str,
                )
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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
