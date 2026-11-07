"""Tests for metric-name regex filtering in /metrics/query."""

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_metric_regex_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from core import app, SessionLocal  # noqa: E402
from db_models import Alarm, AuditLog, MetricNote, Metrics  # noqa: E402

import routes.admin  # noqa: E402,F401
import routes.auth  # noqa: E402,F401
import routes.metrics  # noqa: E402,F401

CONF = os.path.join(TMP, "conf")
os.makedirs(CONF, exist_ok=True)
with open(os.path.join(CONF, "agents.json"), "w", encoding="utf-8") as handle:
    json.dump(
        {
            "agents": {"testagent": {"apikey": "testkey", "enabled": True}},
            "groups": {},
        },
        handle,
    )
with open(os.path.join(CONF, "users.json"), "w", encoding="utf-8") as handle:
    json.dump({"daniel": generate_password_hash("secret")}, handle)

AGENT_HEADERS = {"agentid": "testagent", "X-API-Key": "testkey"}


def _reset_data():
    session = SessionLocal()
    try:
        session.query(MetricNote).delete()
        session.query(Alarm).delete()
        session.query(Metrics).delete()
        session.query(AuditLog).delete()
        session.commit()
    finally:
        session.close()


def _add_metric(name: str):
    session = SessionLocal()
    try:
        row = Metrics(
            agentid="a1",
            pluginid="p1",
            metric=name,
            timestamp=datetime.now(UTC) - timedelta(seconds=1),
            value_float=1.0,
        )
        session.add(row)
        session.commit()
    finally:
        session.close()


def _login(client):
    response = client.post("/login", json={"username": "daniel", "password": "secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


def _names(rows):
    return sorted(row["metric"] for row in rows)


def test_regex_filter_is_case_insensitive():
    _reset_data()
    for name in ("cpu.user", "cpu.system", "disk.used", "network_rx_bytes"):
        _add_metric(name)
    client = app.test_client()
    headers = _login(client)

    response = client.get(
        "/metrics/query",
        query_string={
            "agentid": "a1",
            "pluginid": "p1",
            "metric": r"^CPU\.(user|system)$",
            "metric_regex": "1",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert _names(response.get_json()) == ["cpu.system", "cpu.user"]


def test_plain_substring_filter_still_works():
    _reset_data()
    for name in ("cpu.user", "disk.used"):
        _add_metric(name)
    client = app.test_client()
    headers = _login(client)

    response = client.get(
        "/metrics/query",
        query_string={"agentid": "a1", "pluginid": "p1", "metric": "cpu"},
        headers=headers,
    )
    assert response.status_code == 200
    assert _names(response.get_json()) == ["cpu.user"]

    # Regex metacharacters are literal unless metric_regex is set.
    response = client.get(
        "/metrics/query",
        query_string={"agentid": "a1", "pluginid": "p1", "metric": r"^cpu"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json() == []


def test_invalid_regex_returns_400():
    _reset_data()
    client = app.test_client()
    response = client.get(
        "/metrics/query",
        query_string={"metric": "(", "metric_regex": "1"},
        headers=AGENT_HEADERS,
    )
    assert response.status_code == 400
    assert "invalid regular expression" in response.get_json()["error"]


def main():
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
