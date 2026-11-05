"""Tests for notes on individual stored metric values."""

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_metric_notes_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from core import app, SessionLocal  # noqa: E402
from db_models import Alarm, AuditLog, MetricNote, Metrics  # noqa: E402

import routes.admin  # noqa: E402,F401
import routes.auth  # noqa: E402,F401
import routes.metrics  # noqa: E402,F401
from cleanup_job import _cleanup  # noqa: E402

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

AGENT_HEADERS = {"agentid": "testagent", "X-API-Key": "testkey"}


def _reset_users():
    with open(os.path.join(CONF, "users.json"), "w", encoding="utf-8") as handle:
        json.dump({"daniel": generate_password_hash("secret")}, handle)
    with routes.auth._login_lock:
        routes.auth._login_attempts.clear()


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


def _new_metric(value=42.5, age_days=0):
    session = SessionLocal()
    try:
        metric = Metrics(
            agentid="a1",
            pluginid="p1",
            metric="cpu",
            timestamp=datetime.now(UTC) - timedelta(days=age_days),
            value_float=value,
        )
        session.add(metric)
        session.commit()
        return metric.id
    finally:
        session.close()


def _login(client):
    response = client.post("/login", json={"username": "daniel", "password": "secret"})
    assert response.status_code == 200
    return response.get_json()


def _auth(session):
    return {"Authorization": f"Bearer {session['token']}"}


def test_create_note_and_query_it():
    _reset_users()
    _reset_data()
    client = app.test_client()
    session = _login(client)
    metric_id = _new_metric()

    saved = client.put(
        f"/metrics/{metric_id}/note",
        json={"note": "kernel change documented"},
        headers=_auth(session),
    )
    assert saved.status_code == 200
    body = saved.get_json()
    assert body["note"] == "kernel change documented"
    assert body["note_by"] == "daniel"
    assert body["note_id"]

    rows = client.get(
        "/metrics/query?agentid=a1&pluginid=p1&metric=cpu",
        headers=_auth(session),
    ).get_json()
    assert rows
    assert rows[0]["note"] == "kernel change documented"
    assert rows[0]["note_by"] == "daniel"

    # Agent API keys must not see human notes.
    agent_rows = client.get(
        "/metrics/query?agentid=a1&pluginid=p1&metric=cpu",
        headers=AGENT_HEADERS,
    ).get_json()
    assert agent_rows[0]["note"] is None


def test_update_and_clear_note():
    _reset_users()
    _reset_data()
    client = app.test_client()
    session = _login(client)
    metric_id = _new_metric()

    first = client.put(
        f"/metrics/{metric_id}/note", json={"note": "first"}, headers=_auth(session)
    ).get_json()
    second = client.put(
        f"/metrics/{metric_id}/note", json={"note": "second"}, headers=_auth(session)
    ).get_json()
    assert second["note_id"] == first["note_id"]
    assert second["note"] == "second"

    cleared = client.put(
        f"/metrics/{metric_id}/note", json={"note": ""}, headers=_auth(session)
    ).get_json()
    assert cleared["note"] is None
    assert cleared["note_id"] is None

    rows = client.get(
        "/metrics/query?agentid=a1&pluginid=p1&metric=cpu",
        headers=_auth(session),
    ).get_json()
    assert rows[0]["note"] is None


def test_note_validation_and_unknown_metric():
    _reset_users()
    _reset_data()
    client = app.test_client()
    session = _login(client)
    metric_id = _new_metric()

    assert (
        client.put(
            f"/metrics/{metric_id}/note", json={"note": 123}, headers=_auth(session)
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/metrics/{metric_id}/note",
            json={"note": "x" * 4001},
            headers=_auth(session),
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/metrics/999999/note", json={"note": "x"}, headers=_auth(session)
        ).status_code
        == 404
    )


def test_agent_cannot_write_note():
    _reset_users()
    _reset_data()
    client = app.test_client()
    metric_id = _new_metric()
    response = client.put(
        f"/metrics/{metric_id}/note",
        json={"note": "agent note"},
        headers=AGENT_HEADERS,
    )
    assert response.status_code == 403


def test_note_is_audited_without_text():
    _reset_users()
    _reset_data()
    client = app.test_client()
    session = _login(client)
    metric_id = _new_metric()

    client.put(
        f"/metrics/{metric_id}/note",
        json={"note": "very secret annotation"},
        headers=_auth(session),
    )

    db = SessionLocal()
    try:
        entry = (
            db.query(AuditLog)
            .filter(AuditLog.action == "metric.note")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        assert entry.actor_id == "daniel"
        assert entry.target_type == "metric"
        assert entry.target_id == str(metric_id)
        assert "very secret annotation" not in (entry.details or "")
    finally:
        db.close()


def test_metric_cleanup_removes_notes():
    _reset_users()
    _reset_data()
    client = app.test_client()
    session = _login(client)
    metric_id = _new_metric(age_days=2)

    client.put(
        f"/metrics/{metric_id}/note",
        json={"note": "old note"},
        headers=_auth(session),
    )
    cleaned = client.delete(
        "/admin/maintenance/metrics",
        json={"days": 1, "agent": "a1", "plugin": "p1", "metric": "cpu"},
        headers=_auth(session),
    )
    assert cleaned.status_code == 200
    db = SessionLocal()
    try:
        assert db.query(MetricNote).count() == 0
    finally:
        db.close()


def test_automatic_cleanup_removes_notes():
    _reset_users()
    _reset_data()
    client = app.test_client()
    session = _login(client)
    metric_id = _new_metric(age_days=2)
    client.put(
        f"/metrics/{metric_id}/note",
        json={"note": "old automatic cleanup note"},
        headers=_auth(session),
    )

    deleted_metrics, _deleted_alarms = _cleanup(1, 100)
    assert deleted_metrics == 1
    db = SessionLocal()
    try:
        assert db.query(MetricNote).count() == 0
    finally:
        db.close()


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
