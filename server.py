import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/status", methods=["GET"])
def status():
    agentid = request.headers.get("agentid", "Unknown")
    status = None
    if 'online' in request.args:
        status = 'online'
    elif 'offline' in request.args:
        status = 'offline'
    else:
        status = 'undefined'
    
    print(f"AgentID: {agentid}, Status: {status}")
    return f"AgentID: {agentid}, Status: {status}", 200

@app.route("/plugins", methods=["GET"])
def plugins():
    available_plugins = ["cpu", "ram"]
    return jsonify(available_plugins), 200

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

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
