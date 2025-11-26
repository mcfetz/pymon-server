from flask import Flask, request

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

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
