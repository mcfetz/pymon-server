import os
import toml
import logging
import sqlite3
from flask import Flask, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)
app = Flask(__name__)

def init_db(db_path="metrics.db"):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agentid TEXT,
        pluginid TEXT,
        timestamp REAL,
        metric TEXT,
        value_float REAL,
        value_int INTEGER,
        value_str TEXT
    )
    """)
    conn.commit()
    return conn


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


@app.route("/metric", methods=["POST"])
def collect_metrics():
    # Neuer /metric Endpoint: Payload und agentid-Header ausgeben
    agentid = request.headers.get("agentid", "Unknown")
    payload = request.get_json(silent=True)

    logger.info(f"AgentID: {agentid}")
    logger.info("Received payload: %s", payload)

    # Initialisiere DB-Verbindung und lege Tabelle an, falls nicht vorhanden
    conn = init_db()
    cursor = conn.cursor()

    # Erwarte Payload-Struktur: pluginid, agentid, timestamp, metrics (als Liste)
    pluginid = payload.get("pluginid")
    # agentid aus Payload oder Header (Header hat Vorrang, sofern gewünscht)
    agentid_payload = payload.get("agentid", agentid)
    timestamp = payload.get("timestamp")
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
                else:
                    value_str = str(value)

                cursor.execute("""
                    INSERT INTO metrics (agentid, pluginid, timestamp, metric, value_float, value_int, value_str)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (agentid_payload, pluginid, timestamp, metric_name, value_float, value_int, value_str))

    conn.commit()
    conn.close()

    return jsonify({"status": "Metrics stored"}), 200


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
