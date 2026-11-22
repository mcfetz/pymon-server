"""Tests for the plugin-reported topology graph.

The contract under test: a plugin may report nodes and edges next to its
metrics, and the server keeps them as that plugin's current view of the graph.
What has to hold:

1. A payload upserts the elements it contains, and elements the plugin no
   longer reports disappear - so the tables stay a live snapshot instead of
   growing forever.
2. A payload with neither nodes nor edges is "no information" and changes
   nothing; a plugin hiccup must not blank out its part of the graph.
3. Malformed entries are skipped without raising. Every plugin shares one
   ingest path, so a broken one must not be able to cost the others.
4. Nodes are grouped by GENERIC identifiers, not by plugin-private ids. That is
   the whole point of the design: a plugin that knows a device by MAC and one
   that knows it by IP must end up as the same node. The test below is written
   as the concrete case - unifi sees MAC and address, docker sees address and
   name - because that is what a plugin-specific id scheme silently fails at.
"""

import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_topology_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import SessionLocal  # noqa: E402
from db_models import TopologyEdge, TopologyNode  # noqa: E402
from topology import _as_utc, load_topology, normalize_identifier, store_topology  # noqa: E402

AGENT = "akecbjyf"
MAC_BOOK = "mac:aa:bb:cc:dd:ee:ff"
MAC_UPLINK = "mac:9c:05:d6:a2:1b:2c"


def setup_module(_module):
    _reset()


def teardown_module(_module):
    shutil.rmtree(TMP, ignore_errors=True)


def _reset():
    session = SessionLocal()
    try:
        session.query(TopologyEdge).delete()
        session.query(TopologyNode).delete()
        session.commit()
    finally:
        session.close()


def _payload() -> dict:
    return {
        "v": 2,
        "nodes": [
            {"ids": [MAC_BOOK, "ip:192.168.177.83"], "kind": "client", "label": "macbook"},
            {"ids": [MAC_UPLINK], "kind": "switch", "label": "US 48"},
        ],
        "edges": [
            {"src": MAC_BOOK, "dst": MAC_UPLINK, "kind": "wired", "attrs": {"port": 10}},
        ],
    }


def _close(a: datetime, b: datetime, seconds: float = 1.0) -> bool:
    """Compare timestamps with a tolerance and across naive/aware differences.

    SQLite drops sub-second precision and returns naive datetimes, PostgreSQL
    keeps both, so neither an exact comparison nor a naive subtraction works
    for all backends.
    """
    return abs((_as_utc(a) - _as_utc(b)).total_seconds()) <= seconds


def _node(graph: dict, node_id: str) -> dict:
    matches = [n for n in graph["nodes"] if n["id"] == node_id]
    assert len(matches) == 1, f"expected exactly one node {node_id}, got {graph['nodes']}"
    return matches[0]


def test_normalize_identifier_accepts_what_real_plugins_produce():
    # Case and separators must not matter - that is what lets two plugins agree
    # without coordinating.
    assert normalize_identifier("MAC:AA-BB-CC-DD-EE-FF") == "mac:aa:bb:cc:dd:ee:ff"
    assert normalize_identifier("mac:aa.bb.cc.dd.ee.ff") == "mac:aa:bb:cc:dd:ee:ff"
    assert normalize_identifier("host:Docker12.SRV.") == "host:docker12.srv"
    assert normalize_identifier("ip:192.168.181.022") == "ip:192.168.181.22"
    assert normalize_identifier("endpoint:docker11.SRV:8123") == "endpoint:docker11.srv:8123"
    assert normalize_identifier("endpoint:192.168.181.21:8123") == "endpoint:192.168.181.21:8123"

    # Anything unusable is rejected instead of being stored as a phantom node.
    for bad in (None, "", "   ", "without-type", "foo:bar", "ip:999.1.1.1",
                "ip:192.168.181", "mac:zz:zz", "endpoint:host", "endpoint::abc"):
        assert normalize_identifier(bad) is None, bad


def test_two_plugins_merge_on_a_shared_identifier():
    """The scenario the identifier scheme exists for.

    unifi_topology knows the device by MAC and address, docker_host knows it by
    address and name. Neither knows the other's primary key, but the address is
    common - so the graph must show ONE node with two sources.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "unifi_topology", {
            "v": 2,
            "nodes": [{"ids": [MAC_BOOK, "ip:192.168.179.22"], "kind": "client",
                       "label": "docker12", "attrs": {"switch_port": 6}}],
        }, t0)
        store_topology(session, AGENT, "docker_host", {
            "v": 2,
            "nodes": [{"ids": ["ip:192.168.179.22", "host:docker12.srv"],
                       "kind": "host", "attrs": {"containers": 7}}],
        }, t0 + timedelta(seconds=30))
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        # The most stable identifier names the group: the MAC, not the address.
        node = _node(graph, MAC_BOOK)
        assert node["ids"] == sorted([MAC_BOOK, "ip:192.168.179.22", "host:docker12.srv"])
        assert sorted(node["sources"]) == [f"{AGENT}/docker_host", f"{AGENT}/unifi_topology"]
        # Attributes of both reports survive; the newer report owns last_seen.
        assert node["attrs"] == {"switch_port": 6, "containers": 7}
        assert node["stale"] is False
        # No second node was created for the same device.
        assert len(graph["nodes"]) == 1
    finally:
        session.close()


def test_transitive_merge_and_implicit_endpoints():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        # Plugin A bridges MAC <-> IP, plugin B bridges IP <-> name. Neither
        # alone connects MAC and name; together they form one device.
        store_topology(session, AGENT, "a", {
            "v": 2, "nodes": [{"ids": [MAC_BOOK, "ip:10.0.0.5"], "kind": "client"}],
        }, t0)
        store_topology(session, AGENT, "b", {
            "v": 2,
            "nodes": [{"ids": ["ip:10.0.0.5", "host:web01.lan"], "kind": "client"}],
            "edges": [{"src": "host:web01.lan", "dst": "ip:10.0.0.99", "kind": "talks-to"}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        merged = _node(graph, MAC_BOOK)
        assert merged["ids"] == sorted([MAC_BOOK, "ip:10.0.0.5", "host:web01.lan"])
        assert sorted(merged["sources"]) == [f"{AGENT}/a", f"{AGENT}/b"]

        # The far end of that edge was never described by anyone: it must still
        # exist as a node, otherwise the UI draws an edge into nothing.
        unknown = _node(graph, "ip:10.0.0.99")
        assert unknown["implicit"] is True
        assert unknown["sources"] == []
        assert graph["edges"][0]["src"] == MAC_BOOK
    finally:
        session.close()


def test_endpoint_aliases_join_by_resolved_address():
    """The hop a real chain exposes: one service, reached under two names.

    docker publishes a port and knows its host by name and address; Zoraxy was
    pointed at the same service by address. Both describe the same service, so
    the endpoints must merge - and neither may merge into the host node, whose
    relation to the service is exactly the edge worth drawing.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "unifi_topology", {
            "v": 2,
            "nodes": [{"ids": ["host:docker11.srv", "ip:192.168.181.21"], "kind": "host"}],
        }, t0)
        store_topology(session, AGENT, "docker_host", {
            "v": 2,
            "nodes": [
                {"ids": ["host:docker11.srv"], "kind": "host"},
                {"ids": ["endpoint:192.168.181.21:8123"], "kind": "service", "label": "loopback"},
            ],
        }, t0)
        store_topology(session, AGENT, "zoraxy", {
            "v": 2,
            "nodes": [
                {"ids": ["endpoint:docker11.srv:8123"], "kind": "service"},
                {"ids": ["host:loopback.familie-heise.de"], "kind": "public-name"},
            ],
            "edges": [{"src": "host:loopback.familie-heise.de",
                       "dst": "endpoint:docker11.srv:8123", "kind": "proxies",
                       "attrs": {"port": 8123}}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        services = [n for n in graph["nodes"] if n["kind"] == "service"]
        assert len(services) == 1, graph["nodes"]
        assert services[0]["ids"] == ["endpoint:192.168.181.21:8123", "endpoint:docker11.srv:8123"]

        hosts = [n for n in graph["nodes"] if n["kind"] == "host"]
        assert len(hosts) == 1
        assert hosts[0]["id"] != services[0]["id"], "the service must not merge into its host"

        edge = graph["edges"][0]
        assert edge["src"] == "host:loopback.familie-heise.de"
        assert edge["dst"] == services[0]["id"]
    finally:
        session.close()


def test_abstract_nodes_carry_what_nobody_else_can_know():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "unifi_gateway", {
            "v": 2,
            "nodes": [{"ids": ["abstract:internet"], "kind": "wan", "label": "Internet"}],
            "edges": [{"src": "endpoint:192.168.181.1:443", "dst": "abstract:internet",
                       "kind": "routes-to"}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        internet = _node(graph, "abstract:internet")
        assert internet["kind"] == "wan"
        assert internet["implicit"] is False

        # An abstract name is compared case-insensitively like everything else.
        assert normalize_identifier("abstract:INTERNET") == "abstract:internet"
    finally:
        session.close()


def test_snapshot_upserts_and_prunes():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC).replace(microsecond=123456)
        assert store_topology(session, AGENT, "unifi_topology", _payload(), t0) == (2, 1)
        session.commit()

        # Next poll: one node less, one more edge.
        t1 = t0 + timedelta(minutes=1)
        payload = _payload()
        payload["nodes"] = payload["nodes"][:1]
        payload["edges"].append({"src": MAC_UPLINK, "dst": "mac:00:11:22:33:44:55", "kind": "uplink"})
        assert store_topology(session, AGENT, "unifi_topology", payload, t1) == (1, 2)
        session.commit()

        nodes = session.query(TopologyNode).all()
        assert [n.node_id for n in nodes] == [MAC_BOOK]
        # The node was re-reported: age refreshes, identity is kept.
        assert _close(nodes[0].last_seen, t1)
        assert _close(nodes[0].first_seen, t0)
        assert nodes[0].ids is not None and MAC_BOOK in nodes[0].ids
        assert session.query(TopologyEdge).count() == 2
    finally:
        session.close()


def test_empty_payload_changes_nothing():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "unifi_topology", _payload(), t0)
        session.commit()

        # Neither nodes nor edges: "no information", not "nothing exists".
        assert store_topology(session, AGENT, "unifi_topology", {"v": 2}, t0 + timedelta(minutes=1)) == (0, 0)
        session.commit()
        assert session.query(TopologyNode).count() == 2
        assert session.query(TopologyEdge).count() == 1
    finally:
        session.close()


def test_malformed_entries_are_skipped():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        payload = {
            "v": 2,
            "nodes": [
                {"ids": [MAC_UPLINK], "kind": "switch"},
                {"label": "no ids"},                      # skipped
                "not a dict",                             # skipped
                {"ids": ["   ", "foo:bar"]},              # no usable id, skipped
                {"ids": "mac:aa:bb:cc:dd:ee:ff"},         # single id accepted
            ],
            "edges": [
                {"src": MAC_UPLINK, "dst": "ip:10.0.0.1"},
                {"src": MAC_UPLINK},                      # no dst, skipped
                {"dst": MAC_UPLINK},                      # no src, skipped
                {"src": "unparseable", "dst": MAC_UPLINK},  # bad identifier, skipped
            ],
        }
        # Two well-formed nodes (one of them via a single "ids" string) and one
        # complete edge; everything else is skipped, nothing raises.
        assert store_topology(session, AGENT, "broken", payload, t0) == (2, 1)
        session.commit()
        assert session.query(TopologyNode).count() == 2
        assert session.query(TopologyEdge).count() == 1

        # Non-dict payload and non-list members must not raise either.
        assert store_topology(session, AGENT, "broken", "nonsense", t0) == (0, 0)
        assert store_topology(session, AGENT, "broken", {"nodes": "x", "edges": 5}, t0) == (0, 0)
    finally:
        session.close()


def test_stale_marking_and_source_filter():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC) - timedelta(hours=2)
        store_topology(session, AGENT, "unifi_topology", _payload(), t0)
        session.commit()

        fresh = load_topology(session, stale_after_seconds=900)
        assert all(n["stale"] for n in fresh["nodes"])

        untouched = load_topology(session, stale_after_seconds=86400)
        assert not any(n["stale"] for n in untouched["nodes"])

        # Filtering by a plugin that reported nothing returns an empty graph,
        # not the other plugin's rows.
        other = load_topology(session, pluginid="does_not_exist")
        assert other["nodes"] == [] and other["edges"] == []
    finally:
        session.close()
