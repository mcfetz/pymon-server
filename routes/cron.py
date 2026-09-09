"""Agent-facing cron task endpoints."""

from datetime import datetime, UTC

from flask import jsonify, request

from auth import require_agent_apikey
from core import app
from cron_scheduler import due_tasks_for_agent


@app.route("/cron/due", methods=["GET"])
@require_agent_apikey
def cron_due():
    """
    List cron tasks that are due for the requesting agent right now.

    Each due task carries the shell command of its referenced agent-side
    executor along with a per-task timeout. A task is served at most once
    per minute per agent.
    """
    agentid = request.agentid
    due = due_tasks_for_agent(agentid)
    return jsonify({"tasks": due, "server_time": datetime.now(UTC).isoformat()})