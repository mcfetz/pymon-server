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
4. Two plugins reporting the same node id merge into ONE node with two sources
   - that is the "integrated" part of the integrated graph.
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
from topology import _as_utc, load_topology, store_topology  # noqa: E402

AGENT = "akecbjyf"


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
        "v": 1,
        "nodes": [
            {"id": "unifi:client:aa:bb", "kind": "client", "label": "macbook",
             "attrs": {"ip": "192.168.177.83"}},
            {"id": "unifi:switch:sw1", "kind": "switch", "label": "US 48"},
        ],
        "edges": [
            {"src": "unifi:client:aa:bb", "dst": "unifi:switch:sw1", "kind": "wired",
             "attrs": {"port": 10}},
        ],
    }


def _close(a: datetime, b: datetime, seconds: float = 1.0) -> bool:
    """Compare timestamps with a tolerance and across naive/aware differences.

    SQLite drops sub-second precision and returns naive datetimes, PostgreSQL
    keeps both, so neither an exact comparison nor a naive subtraction works
    for all backends.
    """
    return abs((_as_utc(a) - _as_utc(b)).total_seconds()) <= seconds


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
        payload["edges"].append(
            {"src": "unifi:switch:sw1", "dst": "unifi:gateway:ucg", "kind": "uplink"}
        )
        assert store_topology(session, AGENT, "unifi_topology", payload, t1) == (1, 2)
        session.commit()

        nodes = session.query(TopologyNode).all()
        assert [n.node_id for n in nodes] == ["unifi:client:aa:bb"]
        # The node was re-reported: age refreshes, identity is kept.
        assert _close(nodes[0].last_seen, t1)
        assert _close(nodes[0].first_seen, t0)
        assert nodes[0].attrs is not None and "192.168.177.83" in nodes[0].attrs
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
        assert store_topology(session, AGENT, "unifi_topology", {"v": 1}, t0 + timedelta(minutes=1)) == (0, 0)
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
            "v": 1,
            "nodes": [
                {"id": "ok:host:1", "kind": "host"},
                {"label": "no id"},          # skipped
                "not a dict",                # skipped
                {"id": "   "},               # blank id, skipped
            ],
            "edges": [
                {"src": "ok:host:1", "dst": "ok:svc:1"},
                {"src": "ok:host:1"},        # no dst, skipped
                {"dst": "ok:host:1"},        # no src, skipped
            ],
        }
        # Nodes: only the well-formed one. Edges: only the complete one.
        assert store_topology(session, AGENT, "broken", payload, t0) == (1, 1)
        session.commit()
        assert session.query(TopologyNode).count() == 1
        assert session.query(TopologyEdge).count() == 1

        # Non-dict payload and non-list members must not raise either.
        assert store_topology(session, AGENT, "broken", "nonsense", t0) == (0, 0)
        assert store_topology(session, AGENT, "broken", {"nodes": "x", "edges": 5}, t0) == (0, 0)
    finally:
        session.close()


def test_two_plugins_reporting_the_same_node_are_merged():
    _reset()
    session = SessionLocal()
    try:
        t0 = datetime.now(UTC)
        # The docker plugin knows the host, the unifi plugin knows where it is
        # plugged in. Same node id => one node in the graph, two sources.
        store_topology(session, AGENT, "docker_host", {
            "v": 1,
            "nodes": [{"id": "host:docker12.srv", "kind": "host", "label": "docker12"}],
        }, t0)
        store_topology(session, AGENT, "unifi_topology", {
            "v": 1,
            "nodes": [{"id": "host:docker12.srv", "kind": "host", "attrs": {"switch_port": 6}}],
        }, t0 + timedelta(seconds=30))
        session.commit()

        graph = load_topology(session, stale_after_seconds=3600)
        hosts = [n for n in graph["nodes"] if n["id"] == "host:docker12.srv"]
        assert len(hosts) == 1
        node = hosts[0]
        assert sorted(node["sources"]) == [f"{AGENT}/docker_host", f"{AGENT}/unifi_topology"]
        # Label came from the first report, attrs from the newer one.
        assert node["label"] == "docker12"
        assert node["attrs"] == {"switch_port": 6}
        assert node["stale"] is False
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
