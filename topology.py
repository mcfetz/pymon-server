"""Nodes and edges reported by plugins, for the integrated topology view.

Plugins are standalone scripts: config on stdin, a flat JSON object of metrics
on stdout. This module defines the optional extension of that contract:

    {
      "cpu_percent": 12.5,
      "__topology__": {
        "v": 2,
        "nodes": [
          {"ids": ["mac:aa:bb:cc:dd:ee:ff", "ip:192.168.177.83",
                   "host:macbook.familie-heise.de"],
           "kind": "client", "label": "macbook", "attrs": {"ssid": "home"}}
        ],
        "edges": [
          {"src": "mac:aa:bb:cc:dd:ee:ff", "dst": "mac:9c:05:d6:a2:1b:2c",
           "kind": "wired", "attrs": {"port": 10}}
        ]
      }
    }

The agent strips ``__topology__`` from the metric dict and sends it as
``topology`` in the ``/metrics`` payload; :func:`store_topology` persists it.
A plugin may report *only* ``__topology__`` and no metrics at all.

Identifier model (read this before writing a plugin)
---------------------------------------------------
A node does **not** carry one opaque id. It carries the set of generic
identifiers it is known by - the things *other* plugins can also know:

    mac:aa:bb:cc:dd:ee:ff        hardware address, lowercased, colon separated
    ip:192.168.181.22            address, IPv4 in its normal form
    host:docker12.srv            name, always fully qualified, lowercased
    endpoint:docker12.srv:8123   name or IPv4 literal plus a transport port,
    endpoint:192.168.181.22:8123 used for services rather than hosts
    abstract:internet            something no other plugin can know. The one
                                 place where a private name is correct - and
                                 the one type that never merges on its own.

Two nodes belong to the same device when their identifier sets share at least
one *identical* identifier. The server merges transitively, so one plugin can
bridge what another knows in a different form:

    unifi_topology:  mac:aa:bb…:ff   + ip:192.168.179.22
    docker_host:                       ip:192.168.179.22  + host:docker12.srv

Endpoints get one extra rule, because a service is usually named twice - once
by whoever publishes it, once by whoever fronts it: two ``endpoint`` ids whose
address parts resolve to the same node and whose ports match are the same
service. They merge with each other, never with the host they run on: the
relation between a service and its host is an edge (the published port), not
an identity.

The more generic identifiers a plugin contributes, the more of the graph it
joins - report every identifier you can get cheaply (for a host: MAC, IP and
name). Never invent a plugin-private key; nothing else can ever match it. An
earlier draft used ``unifi:client:<mac>`` style ids, which looked tidy and
could never merge with what proxmox or docker report.

Storage is a live snapshot per (agentid, pluginid). Everything the plugin
reports in a payload is upserted with ``last_seen = now``; rows of the same
plugin that were *not* reported in that payload are deleted. A payload with
neither nodes nor edges is treated as "no information" and leaves the snapshot
untouched: a plugin that briefly reports nothing must not wipe its part of the
graph. Rows age visibly through ``last_seen``, and the API marks rows older
than ``stale_after`` as stale so the UI can grey them out.

The returned graph is grouped by identifier, not by source: each group gets a
deterministic ``id`` (its most stable identifier - MAC before name before
address), the complete ``ids`` list, and the ``sources`` that contributed.
"""
import json
import logging
from datetime import UTC, datetime, timedelta

from db_models import TopologyEdge, TopologyNode

logger = logging.getLogger(__name__)

#: Protocol version a plugin announces in ``__topology__.v``.
TOPOLOGY_VERSION = 2

#: Identifier types a plugin may use. Everything is normalised before it is
#: compared, so plugins never have to agree on letter case or formatting.
#: ``abstract`` is the deliberate exception: it names something nobody else can
#: know (the internet, a cloud service), so a plugin-private key is *correct*
#: there - it must simply never be used for something another plugin can see.
IDENTIFIER_TYPES = ("mac", "ip", "host", "endpoint", "abstract")

#: Preference for the group key, most stable first. A MAC survives a rename and
#: a renumbering, a host name survives a renumbering, an address survives
#: neither - so a group is named after the best identifier it has.
ID_PREFERENCE = ("mac", "host", "ip", "endpoint", "abstract")

#: Which kind describes a merged node best - lower is more descriptive.
#: A node shows up in several plugins' reports (the Proxmox host is also a UniFi
#: client) and the kind decides how it reads and which group it lands in. Same
#: idea as ID_PREFERENCE, only for the descriptive field.
KIND_PREFERENCE = (
    "pve-node",
    "docker-host",
    "reverse-proxy",
    "dsl-modem",
    "unifi-device",
    "service",
    "container",
    "pve-guest",
    "public-name",
    "wan",
    "client",
)


def _is_ipv4(text: str) -> bool:
    """True for a dotted-quad address. Used to pick the ip: namespace."""
    parts = str(text or "").split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _kind_rank(kind: str | None) -> int:
    """Rank a node kind: lower is more descriptive.

    An unknown kind still ranks better than no kind at all - a plugin reporting
    a kind we do not know yet is more informative than a node nobody described.
    Without that distinction a tie would keep the field empty.
    """
    text = (kind or "").strip().lower()
    if not text:
        return len(KIND_PREFERENCE) + 1
    return KIND_PREFERENCE.index(text) if text in KIND_PREFERENCE else len(KIND_PREFERENCE)

#: Upper bounds per plugin and payload. Generous for a homelab, but they keep a
#: broken plugin from filling the database with a runaway node list.
MAX_NODES_PER_PLUGIN = 2000
MAX_EDGES_PER_PLUGIN = 5000

#: Upper bound of identifiers a single node may claim.
MAX_IDS_PER_NODE = 20

#: Fallback staleness threshold (seconds) used when the caller does not set one.
DEFAULT_STALE_AFTER_SECONDS = 900


def normalize_identifier(raw) -> str | None:
    """Canonical form of one identifier, or ``None`` if it is unusable.

    Normalising here - and only here - is what lets two plugins agree without
    coordinating: ``MAC:AA-BB-CC-DD-EE-FF`` and ``mac:aa:bb:cc:dd:ee:ff`` are
    the same device, and a host name is compared without case or trailing dot.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    kind, _, value = text.partition(":")
    kind = kind.strip().lower()
    value = value.strip()
    if not value or kind not in IDENTIFIER_TYPES:
        return None

    if kind == "mac":
        cleaned = value.replace("-", ":").replace(".", ":").lower()
        if len(cleaned.replace(":", "")) not in (12, 16):  # 48-bit and EUI-64
            return None
        return f"mac:{cleaned}"

    if kind == "ip":
        # IPv4 in canonical dotted form; IPv6 is deliberately not guessed at.
        parts = value.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return None
        return "ip:" + ".".join(str(int(p)) for p in parts)

    if kind == "host":
        return f"host:{value.rstrip('.').lower()}"

    if kind == "abstract":
        # A name for something no other plugin can know about. Free-form on
        # purpose, normalised only enough to compare reliably.
        return f"abstract:{value.lower()[:120]}"

    # endpoint:<address>:<port> - address may be a name or an IPv4 literal.
    address, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        return None
    address = address.strip()
    if not address:
        return None
    return f"endpoint:{address.lower()}:{int(port)}"


def _normalize_ids(entry: dict) -> list[str]:
    """Usable identifiers of one node entry, deduplicated and sorted.

    Accepts the ``ids`` list as well as a single ``id`` string, so a plugin
    author who writes the obvious thing is not punished for it.
    """
    raw = entry.get("ids")
    if raw is None:
        raw = entry.get("id")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    out: list[str] = []
    for item in raw:
        ident = normalize_identifier(item)
        if ident and ident not in out:
            out.append(ident)
        if len(out) >= MAX_IDS_PER_NODE:
            break
    return sorted(out)


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


def _load_attrs(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a timestamp read back from the database.

    SQLite returns naive datetimes, PostgreSQL returns aware ones; comparing
    either against ``datetime.now(UTC)`` raises without this.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
        # Still stored: a newer agent may speak a newer version while this
        # server is older. Logged so the mismatch is visible.
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
        ids = _normalize_ids(entry)
        if not ids:
            skipped += 1
            continue
        # The row key is the most stable identifier of the node, the same rule
        # the group key uses - otherwise the two would name the same node
        # differently and the row would be recreated on every poll.
        node_id = min(ids, key=_sort_key)
        row = existing_nodes.get(node_id)
        if row is None:
            row = TopologyNode(agentid=agentid, pluginid=pluginid, node_id=node_id, first_seen=now)
            # Register it right away: a payload may describe the same node twice
            # (two tasks of one swarm service fold onto the same name, for
            # example), and a second insert of that row would violate the unique
            # key - which used to take the whole metric write with it.
            existing_nodes[node_id] = row
            session.add(row)
        if node_id in seen_nodes:
            # Same node, second description in this payload: they ARE one node.
            # Keep every identifier and the first non-empty label/kind.
            merged = set(_row_ids(row)) | set(ids)
            row.ids = json.dumps(sorted(merged))
            row.last_seen = now
            if not row.label:
                row.label = _clean_text(entry.get("label"))
            if not row.kind:
                row.kind = _clean_text(entry.get("kind"))
            continue
        seen_nodes.add(node_id)
        row.ids = json.dumps(ids)
        row.kind = _clean_text(entry.get("kind"))
        row.label = _clean_text(entry.get("label"))
        row.attrs = _dump_attrs(entry.get("attrs"))
        row.last_seen = now

    for entry in edges_raw:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        src = normalize_identifier(entry.get("src"))
        dst = normalize_identifier(entry.get("dst"))
        if not src or not dst:
            skipped += 1
            continue
        kind = _clean_text(entry.get("kind")) or ""
        key = (src, dst, kind)
        if key in seen_edges:
            continue
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


def _row_ids(row) -> list[str]:
    """Identifiers of a stored node row, including the row key itself."""
    try:
        stored = json.loads(row.ids) if row.ids else []
    except (TypeError, ValueError):
        stored = []
    if not isinstance(stored, list):
        stored = []
    ids = [i for i in stored if isinstance(i, str)]
    if row.node_id not in ids:
        ids.insert(0, row.node_id)
    return ids


def _sort_key(identifier: str) -> tuple[int, str]:
    """Ordering that puts the most stable identifier of a group first."""
    kind = identifier.split(":", 1)[0]
    return (ID_PREFERENCE.index(kind) if kind in ID_PREFERENCE else len(ID_PREFERENCE), identifier)


def _build_groups(node_rows) -> dict[str, str]:
    """Union identifiers that were reported together on one node.

    Returns ``identifier -> group key``. The group key is the most stable
    identifier of the group (MAC before name before address), which keeps it
    deterministic across restarts and payload orders.
    """
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        root = item
        while parent[root] != root:
            root = parent[root]
        while parent[item] != root:
            parent[item], item = root, parent[item]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Attach the worse root to the better one, so the group key ends up
        # being the most stable identifier of the group.
        if _sort_key(rb) < _sort_key(ra):
            ra, rb = rb, ra
        parent[rb] = ra

    for row in node_rows:
        for ident in _row_ids(row):
            union(row.node_id, ident)

    # Endpoint aliases: the same service reached by name and by address. Both
    # address forms are identifiers of the same host node, so two endpoints
    # with the same port are the same service - join them. They are *not*
    # joined to the host: a service on a machine is a different thing in the
    # graph than the machine, and the edge between them is the interesting part
    # (typically the published port).
    endpoints: dict[tuple[str, int], str] = {}
    address_group: dict[str, str] = {}
    for ident in list(parent):
        kind, _, value = ident.partition(":")
        if kind == "endpoint":
            address, _, port = value.rpartition(":")
            if address and port.isdigit():
                endpoints[(address, int(port))] = ident
        elif kind in ("ip", "host") and value:
            address_group.setdefault(value, find(ident))

    seen_endpoints: dict[tuple[str, int], str] = {}
    for (address, port), ident in endpoints.items():
        group = address_group.get(address)
        if group is None:
            continue
        key = (group, port)
        if key in seen_endpoints:
            union(seen_endpoints[key], ident)
        else:
            seen_endpoints[key] = ident

    return {ident: find(ident) for ident in list(parent)}


def load_topology(
    session,
    agentid: str | None = None,
    pluginid: str | None = None,
    kind: str | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> dict:
    """Return the merged graph as nodes + edges.

    Nodes are grouped by identifier, so the same device reported by several
    plugins becomes one node with several sources. Edge endpoints run through
    the same mapping; an endpoint that nothing described becomes an implicit
    node rather than a dangling reference.
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

    node_rows = list(node_query)
    edge_rows = list(edge_query)
    if kind is not None:
        node_rows = [row for row in node_rows if row.kind == kind]

    mapping = _build_groups(node_rows)
    nodes: dict[str, dict] = {}

    # Which group does an identifier belong to? Built once from the node rows so
    # an edge can find the host of a port nobody described.
    ident_to_group: dict[str, str] = {}
    for row in node_rows:
        group = mapping.get(row.node_id, row.node_id)
        if not group:
            continue
        ident_to_group.setdefault(group, group)
        if row.node_id:
            ident_to_group.setdefault(row.node_id, group)
        for ident in _row_ids(row):
            if ident:
                ident_to_group.setdefault(ident, group)

    def _resolve_endpoint(ident: str) -> str:
        """Fold an unclaimed endpoint onto its host.

        An endpoint ("endpoint:docker11.srv:5002") is a port on a host, not a
        device. Claims a plugin it - the docker service that publishes the port
        carries it as an identifier - the edge lands on that service, which is
        the interesting hop. If nobody claims it (a NAS or printer port), the
        edge belongs to the host; a node per port would only be noise.
        """
        claimed = ident_to_group.get(ident)
        if claimed is not None:
            return claimed
        kind, _, value = ident.partition(":")
        if kind != "endpoint" or not value:
            return ident
        address = value.rsplit(":", 1)[0] if ":" in value else ""
        if not address:
            return ident
        if f"host:{address}" in ident_to_group:
            return ident_to_group[f"host:{address}"]
        if f"ip:{address}" in ident_to_group:
            return ident_to_group[f"ip:{address}"]
        return f"ip:{address}" if _is_ipv4(address) else f"host:{address}"

    def node_entry(group: str, first: dict | None = None) -> dict:
        entry = nodes.get(group)
        if entry is None:
            entry = {
                "id": group,
                "ids": [group],
                "kind": None,
                "label": None,
                "attrs": {},
                "sources": [],
                "implicit": False,
                "first_seen": None,
                "last_seen": None,
            }
            if first:
                entry.update(first)
            nodes[group] = entry
        return entry

    for row in node_rows:
        group = mapping.get(row.node_id, row.node_id)
        entry = node_entry(group)
        source = f"{row.agentid}/{row.pluginid}"
        if source not in entry["sources"]:
            entry["sources"].append(source)
        for ident in _row_ids(row):
            if ident not in entry["ids"]:
                entry["ids"].append(ident)
        # The most recent report wins for the descriptive fields, so a plugin
        # that knows more about the node can take over.
        if row.last_seen and (entry["last_seen"] is None or row.last_seen >= entry["last_seen"]):
            entry["label"] = row.label or entry["label"]
            if row.attrs:
                entry["attrs"] = {**entry["attrs"], **_load_attrs(row.attrs)}
            entry["last_seen"] = row.last_seen
        # The kind is deliberately NOT a matter of recency: a Proxmox host is
        # also a UniFi client, and "client" must not take over just because
        # UniFi reports later in the cycle. The most descriptive kind wins.
        if row.kind and _kind_rank(row.kind) < _kind_rank(entry["kind"]):
            entry["kind"] = _clean_text(row.kind)
        if row.first_seen and (entry["first_seen"] is None or row.first_seen < entry["first_seen"]):
            entry["first_seen"] = row.first_seen
        if len(entry["ids"]) > MAX_IDS_PER_NODE:
            entry["ids"] = sorted(entry["ids"])[:MAX_IDS_PER_NODE]

    edges: dict[tuple[str, str, str], dict] = {}
    for row in edge_rows:
        # Endpoints nobody described belong to their host rather than becoming a
        # node of their own; a port a docker service publishes stays a service.
        src = _resolve_endpoint(mapping.get(row.src, row.src))
        dst = _resolve_endpoint(mapping.get(row.dst, row.dst))
        if src == dst:
            # Both ends are the same device after grouping - a report that
            # cannot be drawn, not a relation.
            continue
        kind_value = row.kind or ""
        key = (src, dst, kind_value)
        source = f"{row.agentid}/{row.pluginid}"
        entry = edges.get(key)
        if entry is None:
            edges[key] = {
                "src": src,
                "dst": dst,
                "kind": row.kind,
                "label": row.label,
                "attrs": _load_attrs(row.attrs),
                "sources": [source],
                "implicit": False,
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
            }
        else:
            if source not in entry["sources"]:
                entry["sources"].append(source)
            if row.last_seen and (entry["last_seen"] is None or row.last_seen >= entry["last_seen"]):
                entry["label"] = row.label or entry["label"]
                if row.attrs:
                    entry["attrs"] = {**entry["attrs"], **_load_attrs(row.attrs)}
                entry["last_seen"] = row.last_seen
            if row.first_seen and (entry["first_seen"] is None or row.first_seen < entry["first_seen"]):
                entry["first_seen"] = row.first_seen
    # Both endpoints of every edge belong to the graph even when nothing
    # described them; otherwise the UI would draw an edge into nothing.
    for (edge_src, edge_dst, _edge_kind) in edges:
        for endpoint in (edge_src, edge_dst):
            if endpoint not in nodes:
                node_entry(endpoint)["implicit"] = True

    threshold = now if stale_after_seconds is None else now - timedelta(seconds=float(stale_after_seconds))
    for entry in list(nodes.values()) + list(edges.values()):
        # Rows come back naive from SQLite and aware from PostgreSQL; make both
        # comparable with the threshold before deciding who is stale.
        entry["first_seen"] = _as_utc(entry["first_seen"])
        entry["last_seen"] = _as_utc(entry["last_seen"])
        entry["stale"] = bool(entry["last_seen"] and entry["last_seen"] < threshold)
        if "ids" in entry:
            entry["ids"] = sorted(entry["ids"])
        entry["first_seen"] = entry["first_seen"].isoformat() if entry["first_seen"] else None
        entry["last_seen"] = entry["last_seen"].isoformat() if entry["last_seen"] else None

    return {
        "nodes": sorted(nodes.values(), key=lambda n: (n["kind"] or "", n["id"])),
        "edges": sorted(edges.values(), key=lambda e: (e["src"], e["dst"], e["kind"] or "")),
        "generated_at": now.isoformat(),
        "stale_after_seconds": stale_after_seconds,
    }
