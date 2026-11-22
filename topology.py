"""Nodes and edges reported by plugins, for the integrated topology view.

Plugins are standalone scripts: config on stdin, a flat JSON object of metrics
on stdout. This module defines the optional extension of that contract:

    {
      "cpu_percent": 12.5,
      "__topology__": {
        "v": 1,
        "nodes": [{"id": "unifi:client:aa:bb:cc:dd:ee:ff", "kind": "client",
                   "label": "macbook", "attrs": {"ip": "192.168.177.83"}}],
        "edges": [{"src": "unifi:client:aa:bb:cc:dd:ee:ff",
                   "dst": "unifi:switch:9c:05:d6:a2:1b:2c", "kind": "wired",
                   "attrs": {"port": 10}}]
      }
    }

The agent strips ``__topology__`` from the metric dict and sends it as
``topology`` in the ``/metrics`` payload; :func:`store_topology` persists it.
A plugin may report *only* ``__topology__`` and no metrics at all - the graph
is then the whole product of that plugin.

Storage is a live snapshot per (agentid, pluginid). Everything the plugin
reports in a payload is upserted with ``last_seen = now``; rows of the same
plugin that were *not* reported in that payload are deleted, so the tables
never accumulate stale entries. A payload with neither nodes nor edges is
treated as "no information" and leaves the snapshot untouched: a plugin that
briefly reports nothing must not wipe its part of the graph. Rows age visibly
through ``last_seen``, and the API marks rows older than ``stale_after`` as
stale so the UI can grey them out instead of pretending they are live.

Node ids are chosen by the plugin and must be globally unique and stable across
runs (``<namespace>:<kind>:<key>``). They are the merge key of the integrated
graph: two plugins reporting the same id describe the same node, and the API
merges them into one entry with several sources.
"""
import json
import logging
from datetime import UTC, datetime, timedelta

from db_models import TopologyEdge, TopologyNode

logger = logging.getLogger(__name__)

#: Protocol version a plugin announces in ``__topology__.v``.
TOPOLOGY_VERSION = 1

#: Upper bounds per plugin and payload. Generous for a homelab, but they keep a
#: broken plugin from filling the database with a runaway node list.
MAX_NODES_PER_PLUGIN = 2000
MAX_EDGES_PER_PLUGIN = 5000

#: Fallback staleness threshold (seconds) used when the caller does not set one.
DEFAULT_STALE_AFTER_SECONDS = 900


def _clean_id(value) -> str | None:
    """Normalise a plugin-supplied identifier; ``None`` means "skip this entry"."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:200]


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:200] if text else None


def _dump_attrs(value) -> str | None:
    """Serialise an arbitrary attribute dict, dropping anything unserialisable."""
    if not isinstance(value, dict) or not value:
        return None
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        logger.warning("topology: dropping non-serialisable attrs %r", value)
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a timestamp read back from the database.

    SQLite returns naive datetimes, PostgreSQL returns aware ones; comparing
    either against ``datetime.now(UTC)`` raises without this.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _load_attrs(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def store_topology(session, agentid: str, pluginid: str, topology, now: datetime | None = None) -> tuple[int, int]:
    """Persist one plugin's node/edge snapshot.

    Returns ``(nodes, edges)`` - the number of rows reported. Called from the
    metric ingest while ``DB_WRITE_LOCK`` is already held, so it never
    serialises on its own. Malformed entries are skipped with a warning: one
    broken plugin must not take the ingest down with it.
    """
    now = now or datetime.now(UTC)

    if not isinstance(topology, dict):
        logger.warning("topology: ignoring non-dict payload from %s/%s", agentid, pluginid)
        return 0, 0

    version = topology.get("v")
    if version is not None and version != TOPOLOGY_VERSION:
        # Still stored: a newer agent may already speak v2 while this server is
        # older. The version is logged so the mismatch is visible.
        logger.info("topology: %s/%s reports version %r (server speaks %s)", agentid, pluginid, version, TOPOLOGY_VERSION)

    nodes_raw = topology.get("nodes") or []
    edges_raw = topology.get("edges") or []
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        logger.warning("topology: nodes/edges must be lists (%s/%s)", agentid, pluginid)
        return 0, 0

    if len(nodes_raw) > MAX_NODES_PER_PLUGIN or len(edges_raw) > MAX_EDGES_PER_PLUGIN:
        logger.warning(
            "topology: %s/%s reports %d nodes / %d edges, truncating to %d / %d",
            agentid, pluginid, len(nodes_raw), len(edges_raw), MAX_NODES_PER_PLUGIN, MAX_EDGES_PER_PLUGIN,
        )
        nodes_raw = nodes_raw[:MAX_NODES_PER_PLUGIN]
        edges_raw = edges_raw[:MAX_EDGES_PER_PLUGIN]

    existing_nodes = {
        row.node_id: row
        for row in session.query(TopologyNode).filter(
            TopologyNode.agentid == agentid, TopologyNode.pluginid == pluginid
        )
    }
    existing_edges = {
        (row.src, row.dst, row.kind or ""): row
        for row in session.query(TopologyEdge).filter(
            TopologyEdge.agentid == agentid, TopologyEdge.pluginid == pluginid
        )
    }

    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    skipped = 0

    for entry in nodes_raw:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        node_id = _clean_id(entry.get("id"))
        if not node_id:
            skipped += 1
            continue
        seen_nodes.add(node_id)
        row = existing_nodes.get(node_id)
        if row is None:
            row = TopologyNode(agentid=agentid, pluginid=pluginid, node_id=node_id, first_seen=now)
            session.add(row)
        row.kind = _clean_text(entry.get("kind"))
        row.label = _clean_text(entry.get("label"))
        row.attrs = _dump_attrs(entry.get("attrs"))
        row.last_seen = now

    for entry in edges_raw:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        src = _clean_id(entry.get("src"))
        dst = _clean_id(entry.get("dst"))
        if not src or not dst:
            skipped += 1
            continue
        kind = _clean_text(entry.get("kind")) or ""
        key = (src, dst, kind)
        seen_edges.add(key)
        row = existing_edges.get(key)
        if row is None:
            row = TopologyEdge(agentid=agentid, pluginid=pluginid, src=src, dst=dst, kind=kind or None, first_seen=now)
            session.add(row)
        row.label = _clean_text(entry.get("label"))
        row.attrs = _dump_attrs(entry.get("attrs"))
        row.last_seen = now

    if skipped:
        logger.warning("topology: %s/%s skipped %d malformed entries", agentid, pluginid, skipped)

    # Only prune when the plugin actually reported something. An empty payload
    # is "no information yet", not "nothing exists any more".
    if seen_nodes or seen_edges:
        for node_id, row in existing_nodes.items():
            if node_id not in seen_nodes:
                session.delete(row)
        for key, row in existing_edges.items():
            if key not in seen_edges:
                session.delete(row)

    return len(seen_nodes), len(seen_edges)


def load_topology(
    session,
    agentid: str | None = None,
    pluginid: str | None = None,
    kind: str | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> dict:
    """Return the merged graph as nodes + edges.

    Nodes and edges reported by several plugins under the same id are merged
    into one entry that lists its sources; ids are the plugin's responsibility,
    so the merge only ever joins what the plugins agreed on.
    """
    now = now or datetime.now(UTC)
    node_query = session.query(TopologyNode)
    edge_query = session.query(TopologyEdge)
    if agentid is not None:
        node_query = node_query.filter(TopologyNode.agentid == agentid)
        edge_query = edge_query.filter(TopologyEdge.agentid == agentid)
    if pluginid is not None:
        node_query = node_query.filter(TopologyNode.pluginid == pluginid)
        edge_query = edge_query.filter(TopologyEdge.pluginid == pluginid)
    if kind is not None:
        node_query = node_query.filter(TopologyNode.kind == kind)

    nodes: dict[str, dict] = {}
    for row in node_query:
        entry = nodes.get(row.node_id)
        source = f"{row.agentid}/{row.pluginid}"
        if entry is None:
            nodes[row.node_id] = {
                "id": row.node_id,
                "kind": row.kind,
                "label": row.label,
                "attrs": _load_attrs(row.attrs),
                "sources": [source],
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
            }
            continue
        if source not in entry["sources"]:
            entry["sources"].append(source)
        # The most recent report wins for the descriptive fields, so a plugin
        # that knows more about the node can take over.
        if row.last_seen and (entry["last_seen"] is None or row.last_seen >= entry["last_seen"]):
            entry["kind"] = row.kind or entry["kind"]
            entry["label"] = row.label or entry["label"]
            if row.attrs:
                entry["attrs"] = {**entry["attrs"], **_load_attrs(row.attrs)}
            entry["last_seen"] = row.last_seen
        if row.first_seen and (entry["first_seen"] is None or row.first_seen < entry["first_seen"]):
            entry["first_seen"] = row.first_seen

    edges: dict[tuple[str, str, str], dict] = {}
    for row in edge_query:
        key = (row.src, row.dst, row.kind or "")
        source = f"{row.agentid}/{row.pluginid}"
        entry = edges.get(key)
        if entry is None:
            edges[key] = {
                "src": row.src,
                "dst": row.dst,
                "kind": row.kind,
                "label": row.label,
                "attrs": _load_attrs(row.attrs),
                "sources": [source],
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
            }
            continue
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if row.last_seen and (entry["last_seen"] is None or row.last_seen >= entry["last_seen"]):
            entry["label"] = row.label or entry["label"]
            if row.attrs:
                entry["attrs"] = {**entry["attrs"], **_load_attrs(row.attrs)}
            entry["last_seen"] = row.last_seen
        if row.first_seen and (entry["first_seen"] is None or row.first_seen < entry["first_seen"]):
            entry["first_seen"] = row.first_seen

    threshold = now if stale_after_seconds is None else now - timedelta(seconds=float(stale_after_seconds))
    for entry in list(nodes.values()) + list(edges.values()):
        # Rows come back naive from SQLite and aware from PostgreSQL; make both
        # comparable with the threshold before deciding who is stale.
        entry["first_seen"] = _as_utc(entry["first_seen"])
        entry["last_seen"] = _as_utc(entry["last_seen"])
        entry["stale"] = bool(entry["last_seen"] and entry["last_seen"] < threshold)
        entry["first_seen"] = entry["first_seen"].isoformat() if entry["first_seen"] else None
        entry["last_seen"] = entry["last_seen"].isoformat() if entry["last_seen"] else None

    return {
        "nodes": sorted(nodes.values(), key=lambda n: (n["kind"] or "", n["id"])),
        "edges": sorted(edges.values(), key=lambda e: (e["src"], e["dst"], e["kind"] or "")),
        "generated_at": now.isoformat(),
        "stale_after_seconds": stale_after_seconds,
    }
