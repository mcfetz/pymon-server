"""Metric-name lists must come from the ingest tracker, not from a table scan.

Regression for the 0.8-2.1 s dropdown calls: both name endpoints
(`GET /agents/<agent>/plugins/<plugin>/metrics` and `GET /metrics/names/<plugin>`)
answered with a DISTINCT over `metrics`, i.e. an index scan over the whole
retention window (measured 2026-11-23 against the live DB: 1953 ms / 272,875
index entries for one agent+plugin pair, 437 ms across all agents of a plugin,
~860 dirty pages written back per call). The list is now read from
`_metric_last_seen`, which holds one row per (agentid, pluginid, metric).

The decisive assertion is that the `metrics` table is only touched by the
`min(timestamp)` probe deriving the retention cutoff — everything else must come
from the tracker.

Run with:  venv/bin/python tests/test_metric_names.py
"""

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_metric_names_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

from core import SessionLocal, app, engine  # noqa: E402
from db_models import MetricLastSeen, Metrics  # noqa: E402

import routes.agents  # noqa: E402,F401
import routes.metrics  # noqa: E402,F401

CONF = os.path.join(TMP, "conf")
os.makedirs(CONF, exist_ok=True)
with open(os.path.join(CONF, "agents.json"), "w", encoding="utf-8") as handle:
    json.dump(
        {
            "agents": {
                "a1": {
                    "apikey": "key1",
                    "enabled": True,
                    "plugins": {"p1": {"sleep": 60}},
                },
                "a2": {
                    "apikey": "key2",
                    "enabled": True,
                    "plugins": {"p1": {"sleep": 60}},
                },
            },
            "groups": {},
        },
        handle,
    )

HEADERS = {"agentid": "a1", "X-API-Key": "key1"}
NOW = datetime.now(UTC)


def _reset_data():
    session = SessionLocal()
    try:
        session.query(Metrics).delete()
        session.query(MetricLastSeen).delete()
        session.commit()
    finally:
        session.close()


def _add_metric(agentid, pluginid, name, age_seconds=0):
    session = SessionLocal()
    try:
        session.add(
            Metrics(
                agentid=agentid,
                pluginid=pluginid,
                metric=name,
                timestamp=NOW - timedelta(seconds=age_seconds),
                received_at=NOW - timedelta(seconds=age_seconds),
                value_float=1.0,
            )
        )
        session.commit()
    finally:
        session.close()


def _add_tracker(agentid, pluginid, name, age_days=0):
    session = SessionLocal()
    try:
        session.add(
            MetricLastSeen(
                agentid=agentid,
                pluginid=pluginid,
                metric=name,
                last_received_at=NOW - timedelta(days=age_days),
            )
        )
        session.commit()
    finally:
        session.close()


@contextmanager
def _captured_sql():
    """Collect every statement the engine executes inside the block."""
    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _capture)


def _metrics_table_scans(statements):
    """Statements reading `metrics` other than the retention-cutoff probe."""
    return [
        statement
        for statement in statements
        if "from metrics" in statement.lower() and "min(" not in statement.lower()
    ]


def test_agent_plugin_names_do_not_scan_metrics():
    _reset_data()
    for name in ("cpu", "mem", "disk"):
        _add_metric("a1", "p1", name)
        _add_tracker("a1", "p1", name)

    client = app.test_client()
    with _captured_sql() as statements:
        response = client.get("/agents/a1/plugins/p1/metrics", headers=HEADERS)
        assert response.status_code == 200, response.get_json()
        assert response.get_json() == ["cpu", "disk", "mem"], response.get_json()

    scans = _metrics_table_scans(statements)
    assert not scans, f"the name list still scans the metrics table: {scans}"


def test_plugin_names_do_not_scan_metrics():
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_tracker("a1", "p1", "cpu")
    _add_metric("a2", "p1", "mem")
    _add_tracker("a2", "p1", "mem")

    client = app.test_client()
    with _captured_sql() as statements:
        response = client.get("/metrics/names/p1", headers=HEADERS)
        assert response.status_code == 200, response.get_json()
        assert response.get_json() == ["cpu", "mem"], response.get_json()

    scans = _metrics_table_scans(statements)
    assert not scans, f"the name list still scans the metrics table: {scans}"


def test_names_without_data_are_dropped():
    """The tracker is never pruned: names whose rows the cleanup deleted must go."""
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_tracker("a1", "p1", "cpu")
    # Name of a container that stopped reporting long before the retention window.
    _add_tracker("a1", "p1", "container:gone.1:mem_percent", age_days=40)

    client = app.test_client()
    response = client.get("/agents/a1/plugins/p1/metrics", headers=HEADERS)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == ["cpu"], response.get_json()


def test_plugin_scope_drops_names_without_data_too():
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_tracker("a1", "p1", "cpu")
    _add_tracker("a2", "p1", "container:gone.1:mem_percent", age_days=40)

    client = app.test_client()
    response = client.get("/metrics/names/p1", headers=HEADERS)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == ["cpu"], response.get_json()


def test_pair_scope_stays_agent_specific():
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_tracker("a1", "p1", "cpu")
    _add_metric("a2", "p1", "mem")
    _add_tracker("a2", "p1", "mem")

    client = app.test_client()
    response = client.get("/agents/a1/plugins/p1/metrics", headers=HEADERS)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == ["cpu"], response.get_json()


def test_fallback_when_tracker_has_no_rows():
    """A metric that never reached the tracker must still be listed."""
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_metric("a1", "p1", "mem")

    client = app.test_client()
    response = client.get("/agents/a1/plugins/p1/metrics", headers=HEADERS)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == ["cpu", "mem"], response.get_json()


def test_unknown_scope_is_empty():
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_tracker("a1", "p1", "cpu")

    client = app.test_client()
    response = client.get("/agents/a1/plugins/nope/metrics", headers=HEADERS)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == [], response.get_json()


def test_endpoints_require_agent_apikey():
    _reset_data()
    _add_metric("a1", "p1", "cpu")
    _add_tracker("a1", "p1", "cpu")

    client = app.test_client()
    for path in ("/agents/a1/plugins/p1/metrics", "/metrics/names/p1"):
        assert client.get(path).status_code == 400, path
        assert (
            client.get(path, headers={"agentid": "a1", "X-API-Key": "nope"}).status_code
            == 401
        ), path


def main():
    print("metric name list tests")
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failed = []
    try:
        for name, test in tests:
            try:
                test()
                print(f"  PASS  {name}")
            except Exception as exc:  # noqa: BLE001 - report, do not abort the run
                failed.append(name)
                print(f"  FAIL  {name}: {exc}")
        print()
        print(f"{len(tests) - len(failed)}/{len(tests)} bestanden")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
