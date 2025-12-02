from core import app, logger
from flask import request
from auth import require_agent_apikey


@app.route("/agents/status", methods=["GET"])
@require_agent_apikey
def status():
    """
    Get agent status.
    ---
    tags:
      - status
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
      - in: query
        name: online
        required: false
        schema:
          type: string
        description: If present, status will be set to "online"
      - in: query
        name: offline
        required: false
        schema:
          type: string
        description: If present, status will be set to "offline"
    responses:
      200:
        description: Current status of the agent
        content:
          text/plain:
            schema:
              type: string
      401:
        description: Invalid or missing API key
    """
    agentid = request.agentid
    status = None
    if "online" in request.args:
        status = "online"
    elif "offline" in request.args:
        status = "offline"
    else:
        status = "undefined"

    logger.info(f"AgentID: {agentid}, Status: {status}")
    return f"AgentID: {agentid}, Status: {status}", 200
