import os
import toml
import importlib
import sys
from flask import Flask, request, jsonify

app = Flask(__name__)


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

    print(f"AgentID: {agentid}, Status: {status}")
    return f"AgentID: {agentid}, Status: {status}", 200


@app.route("/plugins", methods=["GET"])
def plugins():
    agentid = request.headers.get("agentid", None)

    # Lade Konfiguration aus config.toml
    try:
        config = toml.load("config.toml")
    except Exception as e:
        return jsonify({"error": f"Fehler beim Laden der Konfiguration: {str(e)}"}), 500

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
        with open(plugin_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return jsonify({"error": f"Fehler beim Lesen des Plugins: {str(e)}"}), 500

    # Rückgabe des Inhalts als reinen Text
    return content, 200, {"Content-Type": "text/plain"}


@app.route("/metric", methods=["POST"])
def collect_metrics():
    plugins_folder = "plugins"
    metrics_data = {}

    # Alle .py-Dateien im plugins-Ordner durchgehen
    try:
        files = os.listdir(plugins_folder)
    except Exception as e:
        return jsonify({"error": f"Fehler beim Zugriff auf den Plugins-Ordner: {str(e)}"}), 500

    # Filter: Python-Dateien, die nicht (__init__.py und plugin_base.py) sind
    plugin_files = [os.path.splitext(f)[0] for f in files 
                    if f.endswith(".py") and f not in ["__init__.py", "plugin_base.py"]]

    for plugin_name in plugin_files:
        try:
            # Dynamischen Import durchführen, z.B. "plugins.cpu" für cpu.py
            module = importlib.import_module(f"plugins.{plugin_name}")

            # In jedem Modul wird angenommen, dass die Plugin-Klasse als einziges (und korrekt benanntes) Attribut vorhanden ist.
            # Hier wird über alle Attributnamen iteriert und gesucht, ob das Attribut von PluginBase erbt.
            plugin_instance = None
            for attribute_name in dir(module):
                attribute = getattr(module, attribute_name)
                try:
                    # Überprüfe, ob es sich um eine Klasse handelt, die von PluginBase erbt.
                    if isinstance(attribute, type) and issubclass(attribute, sys.modules["plugins.plugin_base"].PluginBase) and attribute is not sys.modules["plugins.plugin_base"].PluginBase:
                        plugin_instance = attribute()
                        break
                except Exception:
                    continue

            if plugin_instance is None:
                continue

            # Hole die Metriken vom Plugin
            metrics = plugin_instance.get_metrics()
            # Verwende die Plugin-ID als Schlüssel
            metrics_data[plugin_instance.get_plugin_id()] = metrics

        except Exception as e:
            # Fehler beim Laden/Instanziieren/Erfassen der Metriken
            metrics_data[plugin_name] = f"Fehler: {str(e)}"

    return jsonify(metrics_data), 200

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
