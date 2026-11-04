"""Tests for the alarm note API (notes on open alarms)."""

import json
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="pymon_alarm_notes_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from core import app, SessionLocal  # noqa: E402
from db_models import Alarm, Metrics  # noqa: E402

import routes.alarms  # noqa: E402,F401
import routes.auth  # noqa: E402,F401

os.makedirs(os.path.join(TMP, "conf"), exist_ok=True)
with open(os.path.join(TMP, "conf", "agents.json"), "w", encoding="utf-8") as handle:
    json.dump(
        {
            "agents": {
                "testagent": {"apikey": "testkey", "enabled": True},
            },
            "groups": {},
        },
        handle,
    )
with open(os.path.join(TMP, "conf", "users.json"), "w", encoding="utf-8") as handle:
    json.dump({"tester": generate_password_hash("secret")}, handle)

HEADERS = {"agentid": "testagent", "X-API-Key": "testkey"}


def _new_alarm():
    session = SessionLocal()
    try:
        session.query(Alarm).delete()
        session.query(Metrics).delete()
        session.commit()
        metric = Metrics(
            agentid="testagent",
            pluginid="p1",
            metric="cpu",
            value_float=99.0,
        )
        session.add(metric)
        session.flush()
        alarm = Alarm(
            agentid="testagent",
            rule_id="r1",
            pluginid="p1",
            metric="cpu",
            severity="warning",
            value=99.0,
            metrics_id=metric.id,
        )
        session.add(alarm)
        session.commit()
        return alarm.id
    finally:
        session.close()


def test_save_note_on_open_alarm():
    client = app.test_client()
    alarm_id = _new_alarm()
    response = client.put(
        f"/alarms/{alarm_id}/note",
        json={"note": "  waiting for ISP  "},
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "saved"
    assert data["note"] == "waiting for ISP"
    assert data["note_updated_at"]

    detail = client.get(f"/alarms/{alarm_id}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.get_json()["note"] == "waiting for ISP"

    listed = client.get("/alarms?acknowledged=false", headers=HEADERS)
    assert listed.status_code == 200
    assert any(
        item["id"] == alarm_id and item["note"] == "waiting for ISP"
        for item in listed.get_json()
    )


def test_save_note_with_frontend_jwt():
    client = app.test_client()
    login = client.post("/login", json={"username": "tester", "password": "secret"})
    assert login.status_code == 200
    token = login.get_json()["token"]

    alarm_id = _new_alarm()
    response = client.put(
        f"/alarms/{alarm_id}/note",
        json={"note": "saved from the UI"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.get_json()["note"] == "saved from the UI"


def test_update_and_clear_note():
    client = app.test_client()
    alarm_id = _new_alarm()
    client.put(f"/alarms/{alarm_id}/note", json={"note": "first"}, headers=HEADERS)
    updated = client.put(
        f"/alarms/{alarm_id}/note", json={"note": "second"}, headers=HEADERS
    )
    assert updated.get_json()["note"] == "second"

    cleared = client.put(f"/alarms/{alarm_id}/note", json={"note": ""}, headers=HEADERS)
    assert cleared.status_code == 200
    assert cleared.get_json()["note"] is None
    assert cleared.get_json()["note_updated_at"] is None


def test_list_endpoint_truncates_note_but_detail_keeps_full_text():
    client = app.test_client()
    alarm_id = _new_alarm()
    long_note = "x" * 300
    client.put(f"/alarms/{alarm_id}/note", json={"note": long_note}, headers=HEADERS)

    listed = client.get("/alarms?acknowledged=false", headers=HEADERS).get_json()
    preview = next(item["note"] for item in listed if item["id"] == alarm_id)
    assert preview.endswith("…")
    assert len(preview) == 201

    detail = client.get(f"/alarms/{alarm_id}", headers=HEADERS).get_json()
    assert detail["note"] == long_note


def test_acknowledged_alarm_note_is_read_only():
    client = app.test_client()
    alarm_id = _new_alarm()
    client.put(
        f"/alarms/{alarm_id}/note", json={"note": "why snoozed"}, headers=HEADERS
    )
    ack = client.get(f"/alarms/{alarm_id}/ack", headers=HEADERS)
    assert ack.status_code == 200

    detail = client.get(f"/alarms/{alarm_id}", headers=HEADERS).get_json()
    assert detail["acknowledged"] is True
    assert detail["note"] == "why snoozed"

    rejected = client.put(
        f"/alarms/{alarm_id}/note", json={"note": "too late"}, headers=HEADERS
    )
    assert rejected.status_code == 409


def test_note_validation():
    client = app.test_client()
    alarm_id = _new_alarm()
    assert (
        client.put(
            f"/alarms/{alarm_id}/note", json={"note": 123}, headers=HEADERS
        ).status_code
        == 400
    )
    assert (
        client.put(
            f"/alarms/{alarm_id}/note",
            json={"note": "x" * 4001},
            headers=HEADERS,
        ).status_code
        == 400
    )


def test_note_unknown_alarm():
    client = app.test_client()
    response = client.put("/alarms/999999/note", json={"note": "x"}, headers=HEADERS)
    assert response.status_code == 404


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
