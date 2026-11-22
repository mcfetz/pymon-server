"""HTTP surface of the topology view.

The graph is assembled from what the plugins report (see ``topology.py``); this
module only exposes the merged result. It uses the same decorator as the metric
read routes, which accepts an agent API key as well as a logged-in frontend
user, so the web UI can read it with its own session.
"""
import logging

from flask import jsonify, request

from auth import require_agent_apikey
from core import SessionLocal, app
from topology import DEFAULT_STALE_AFTER_SECONDS, load_topology

logger = logging.getLogger(__name__)


@app.route("/topology", methods=["GET"])
@require_agent_apikey
def get_topology():
    """
    Merged node/edge graph built from all plugins' topology reports.
    ---
    tags:
      - topology
    parameters:
      - in: header
        name: agentid
        required: true
        schema:
          type: string
      - in: header
        name: X-API-Key
        required: true
        schema:
          type: string
      - in: query
        name: agentid
        schema:
          type: string
        description: Only include rows reported by this agent
      - in: query
        name: pluginid
        schema:
          type: string
        description: Only include rows reported by this plugin
      - in: query
        name: kind
        schema:
          type: string
        description: Only include nodes of this kind
      - in: query
        name: stale_after
        schema:
          type: integer
        description: Seconds after which an element counts as stale (default 900)
    responses:
      200:
        description: Nodes and edges, merged by id
      400:
        description: stale_after is not an integer
      401:
        description: Invalid or missing API key
    """
    agentid = request.args.get("agentid") or None
    pluginid = request.args.get("pluginid") or None
    kind = request.args.get("kind") or None

    raw_stale = request.args.get("stale_after")
    if raw_stale is None:
        stale_after = DEFAULT_STALE_AFTER_SECONDS
    else:
        try:
            stale_after = int(raw_stale)
        except (TypeError, ValueError):
            return jsonify({"error": "stale_after must be an integer number of seconds"}), 400
        if stale_after < 0:
            return jsonify({"error": "stale_after must not be negative"}), 400

    session = SessionLocal()
    try:
        return jsonify(
            load_topology(
                session,
                agentid=agentid,
                pluginid=pluginid,
                kind=kind,
                stale_after_seconds=stale_after,
            )
        )
    except Exception as e:  # pragma: no cover - defensive, the query is simple
        logger.error("Error building topology: %s", e)
        return jsonify({"error": "failed to build topology"}), 500
    finally:
        session.close()
