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


def test_merged_node_keeps_the_most_descriptive_kind():
    """The Proxmox host is also a UniFi client and must stay a pve-node.

    The kind used to come from whichever plugin reported last in the cycle, so
    the node flipped to "client" and landed in the network group instead of the
    Proxmox group.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "proxmox", {
            "v": 2,
            "nodes": [{"ids": ["host:pve3", "host:pve3.srv"], "kind": "pve-node", "label": "pve3"}],
        }, t0)
        # UniFi reports the same machine a minute later, as a client.
        store_topology(session, AGENT, "unifi_gateway", {
            "v": 2,
            "nodes": [{"ids": ["host:pve3.srv", "ip:192.168.177.30"], "kind": "client",
                       "label": "pve3.srv*"}],
        }, t0 + timedelta(minutes=1))
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        assert len(graph["nodes"]) == 1, graph["nodes"]
        node = graph["nodes"][0]
        assert node["kind"] == "pve-node", node
        assert sorted(node["sources"]) == [f"{AGENT}/proxmox", f"{AGENT}/unifi_gateway"], node
        # The label still follows the newest report: that is where Daniel's
        # asterisk notation lives.
        assert node["label"] == "pve3.srv*", node
    finally:
        session.close()


def test_unclaimed_endpoint_folds_onto_its_host():
    """A port is not a device: nobody claims it, so the edge belongs to the host.

    A NAS or printer port used to become a node of its own ("unbekannt"). It is
    a port on a host that is already in the graph, so the edge goes there.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "unifi_gateway", {
            "v": 2,
            "nodes": [{"ids": ["host:diskstation.srv", "ip:192.168.177.40"], "kind": "client",
                       "label": "diskstation"}],
        }, t0)
        store_topology(session, AGENT, "zoraxy", {
            "v": 2,
            "nodes": [{"ids": ["host:calendar.familie-heise.de"], "kind": "public-name"}],
            "edges": [{"src": "host:calendar.familie-heise.de",
                       "dst": "endpoint:diskstation.srv:20003", "kind": "proxies",
                       "attrs": {"port": 20003}}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        by_id = {n["id"]: n for n in graph["nodes"]}
        assert "endpoint:diskstation.srv:20003" not in by_id, list(by_id)
        assert "host:diskstation.srv" in by_id, list(by_id)
        edge = graph["edges"][0]
        assert edge["dst"] == "host:diskstation.srv", edge
        assert edge["attrs"]["port"] == 20003, edge
    finally:
        session.close()


def test_claimed_endpoint_stays_the_service_that_publishes_it():
    """A docker service claims the port, so the public name reaches the service.

    This is the hop worth drawing: name -> service -> host. Only when nobody
    claims the port does the edge fall back to the host.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "docker_host", {
            "v": 2,
            "nodes": [
                {"ids": ["host:docker11.srv"], "kind": "docker-host"},
                {"ids": ["abstract:docker:docker11/bgmon", "endpoint:docker11.srv:5002"],
                 "kind": "service", "label": "bgmon"},
            ],
        }, t0)
        store_topology(session, AGENT, "zoraxy", {
            "v": 2,
            "nodes": [{"ids": ["host:bgmon.familie-heise.de"], "kind": "public-name"}],
            "edges": [{"src": "host:bgmon.familie-heise.de",
                       "dst": "endpoint:docker11.srv:5002", "kind": "proxies",
                       "attrs": {"port": 5002}}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        by_id = {n["id"]: n for n in graph["nodes"]}
        # Which identifier names the group follows ID_PREFERENCE (endpoint beats
        # abstract), so find the service by the endpoint it claims.
        services = [n for n in graph["nodes"] if "endpoint:docker11.srv:5002" in n["ids"]]
        assert len(services) == 1, list(by_id)
        assert services[0]["kind"] == "service", services[0]
        edge = graph["edges"][0]
        assert edge["dst"] == services[0]["id"], edge
    finally:
        session.close()


def test_endpoint_alias_meets_the_service_across_spellings():
    """Same port, two spellings: docker reports the address, the rule the name.

    The docker plugin knows its host by address, a proxy rule points at the port
    by name. Without resolving the name first the edge falls back to the host and
    the service hop - the one worth drawing - would be missed.
    """
    import topology as topology_module

    _reset()
    session = SessionLocal()
    original = topology_module._resolve_address
    topology_module._resolve_address = (
        lambda address: "192.168.181.21" if address == "docker11.srv" else None
    )
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "docker_host", {
            "v": 2,
            "nodes": [
                {"ids": ["host:docker11.srv"], "kind": "docker-host"},
                {"ids": ["abstract:docker:docker11/bgmon", "endpoint:192.168.181.21:5002"],
                 "kind": "service", "label": "bgmon"},
            ],
        }, t0)
        store_topology(session, AGENT, "zoraxy", {
            "v": 2,
            "nodes": [{"ids": ["host:bgmon.familie-heise.de"], "kind": "public-name"}],
            "edges": [{"src": "host:bgmon.familie-heise.de",
                       "dst": "endpoint:docker11.srv:5002", "kind": "proxies",
                       "attrs": {"port": 5002}}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        services = [n for n in graph["nodes"] if "endpoint:192.168.181.21:5002" in n["ids"]]
        assert len(services) == 1, graph["nodes"]
        assert graph["edges"][0]["dst"] == services[0]["id"], graph["edges"][0]
    finally:
        topology_module._resolve_address = original
        session.close()


def test_unknown_kind_still_beats_no_kind():
    """A kind we do not know yet must still fill the field.

    Otherwise it ranks equal to "no kind" and the node ends up with kind None -
    one comparison that silently does nothing.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        store_topology(session, AGENT, "unifi_topology", {
            "v": 2,
            "nodes": [{"ids": ["host:docker11.srv"], "kind": "host"}],
        }, t0)
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        assert graph["nodes"][0]["kind"] == "host", graph["nodes"]
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


def test_one_payload_may_describe_a_node_twice():
    """Two tasks of one swarm service fold onto the same name.

    That used to insert the same row twice: the second insert violated the
    unique key, and because the failure poisoned the surrounding transaction the
    whole metric write of that agent was rolled back - the agent looked healthy
    while its topology silently never arrived.
    """
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        payload = {
            "v": 2,
            "nodes": [
                {"ids": ["host:docker12", "ip:192.168.179.22"], "kind": "docker-host", "label": "docker12"},
                # Two tasks of one published swarm service: same host, same port,
                # same image - so both descriptions carry the very same
                # identifiers, and therefore the very same row key.
                {"ids": ["endpoint:docker12:8080", "abstract:docker:docker12/bgmon"],
                 "kind": "service", "label": "bgmon"},
                {"ids": ["endpoint:docker12:8080", "abstract:docker:docker12/bgmon"],
                 "kind": "service", "label": "bgmon"},
            ],
            "edges": [
                {"src": "host:docker12", "dst": "abstract:docker:docker12/bgmon", "kind": "runs"},
                {"src": "host:docker12", "dst": "abstract:docker:docker12/bgmon", "kind": "runs"},
            ],
        }
        store_topology(session, AGENT, "docker_host", payload, t0)
        session.commit()  # used to raise IntegrityError here

        # The decisive number: the duplicate must not become a second row. The
        # graph alone cannot show it, because load_topology merges rows that
        # carry the same identifier anyway.
        from sqlalchemy import text

        rows = session.execute(text("SELECT count(*) FROM topology_nodes")).scalar()
        assert rows == 2, f"expected 2 node rows (host + service), got {rows}"

        graph = load_topology(session, stale_after_seconds=3600)
        services = [n for n in graph["nodes"] if n["kind"] == "service"]
        assert len(services) == 1, graph["nodes"]
        # Both descriptions survive as identifiers of the one node.
        assert set(services[0]["ids"]) >= {"endpoint:docker12:8080",
                                           "abstract:docker:docker12/bgmon"}, services[0]["ids"]
        assert len(graph["nodes"]) == 2, graph["nodes"]  # the host plus the service
        assert len(graph["edges"]) == 1, graph["edges"]

        # Repeating the same snapshot updates, it does not insert again.
        store_topology(session, AGENT, "docker_host", payload, t0 + timedelta(seconds=5))
        session.commit()
        graph = load_topology(session, stale_after_seconds=3600)
        assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
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


if __name__ == "__main__":
    # The CI runs every tests/test_*.py as a standalone script (no pytest): it
    # expects an "x/y bestanden" line and a non-zero exit on failure. Without
    # this block the file imports cleanly and silently tests nothing there.
    import sys as _sys

    _names = sorted(n for n in dir() if n.startswith("test_"))
    _failed = []
    setup_module(None)
    for _name in _names:
        try:
            globals()[_name]()
        except Exception as _exc:
            _failed.append(_name)
            print(f"  FAIL {_name}: {type(_exc).__name__}: {_exc}")
        else:
            print(f"  PASS {_name}")
    teardown_module(None)
    print(f"{len(_names) - len(_failed)}/{len(_names)} bestanden")
    _sys.exit(1 if _failed else 0)
