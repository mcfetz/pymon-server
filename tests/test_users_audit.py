"""Tests for user roles, user management and the audit trail."""

import json
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="pymon_users_audit_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from core import app, SessionLocal  # noqa: E402
from db_models import Alarm, AuditLog, Metrics  # noqa: E402

import routes.admin  # noqa: E402,F401
import routes.alarms  # noqa: E402,F401
import routes.audit  # noqa: E402,F401
import routes.auth  # noqa: E402,F401
import routes.users  # noqa: E402,F401

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
        session.query(Alarm).delete()
        session.query(Metrics).delete()
        session.query(AuditLog).delete()
        session.commit()
    finally:
        session.close()


def _new_alarm():
    session = SessionLocal()
    try:
        metric = Metrics(
            agentid="testagent", pluginid="p1", metric="cpu", value_float=99.0
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


def _login(client, username, password):
    response = client.post("/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _auth(session):
    return {"Authorization": f"Bearer {session['token']}"}


def test_legacy_user_is_admin_and_login_returns_role():
    _reset_users()
    client = app.test_client()
    session = _login(client, "daniel", "secret")
    assert session["role"] == "admin"

    me = client.get("/account", headers=_auth(session))
    assert me.status_code == 200
    assert me.get_json()["role"] == "admin"


def test_admin_creates_user_and_user_can_log_in():
    _reset_users()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")

    created = client.post(
        "/admin/users",
        json={
            "username": "hermes",
            "password": "hermespw",
            "role": "user",
            "display_name": "Hermes Agent",
        },
        headers=_auth(admin),
    )
    assert created.status_code == 201
    assert created.get_json()["role"] == "user"

    hermes = _login(client, "hermes", "hermespw")
    assert hermes["role"] == "user"
    assert hermes["display_name"] == "Hermes Agent"


def test_user_role_cannot_manage_users():
    _reset_users()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")
    client.post(
        "/admin/users",
        json={"username": "hermes", "password": "hermespw", "role": "user"},
        headers=_auth(admin),
    )
    hermes = _login(client, "hermes", "hermespw")

    listed = client.get("/admin/users", headers=_auth(hermes))
    assert listed.status_code == 403

    agents = client.get("/admin/agents", headers=_auth(hermes)).get_json()
    assert agents["testagent"].get("apikey") is None
    admin_agents = client.get("/admin/agents", headers=_auth(admin)).get_json()
    assert admin_agents["testagent"].get("apikey") == "testkey"


def test_user_can_edit_config_but_not_maintenance():
    _reset_users()
    _reset_data()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")
    client.post(
        "/admin/users",
        json={"username": "hermes", "password": "hermespw", "role": "user"},
        headers=_auth(admin),
    )
    hermes = _login(client, "hermes", "hermespw")

    rule = {
        "id": "r1",
        "title": "CPU test",
        "agentid": ["testagent"],
        "pluginid": "p1",
        "metric": "cpu",
        "condition": "gt",
        "threshold": 90,
        "scope": "single",
        "fire": "single",
        "severity": "warning",
    }
    saved = client.put("/admin/rules/r1", json=rule, headers=_auth(hermes))
    assert saved.status_code == 200

    vacuum = client.post("/admin/maintenance/vacuum", headers=_auth(hermes))
    assert vacuum.status_code == 403

    plugin_source = client.put(
        "/admin/plugins/example/source",
        data="print('x')",
        headers=_auth(hermes),
    )
    assert plugin_source.status_code == 403


def test_user_can_ack_alarm_and_audit_records_actor():
    _reset_users()
    _reset_data()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")
    client.post(
        "/admin/users",
        json={"username": "hermes", "password": "hermespw", "role": "user"},
        headers=_auth(admin),
    )
    hermes = _login(client, "hermes", "hermespw")
    alarm_id = _new_alarm()

    ack = client.get(f"/alarms/{alarm_id}/ack", headers=_auth(hermes))
    assert ack.status_code == 200

    detail = client.get(f"/alarms/{alarm_id}", headers=_auth(hermes)).get_json()
    assert detail["acknowledged_by"] == "hermes"
    assert detail["acknowledged_source"] == "user"

    audit = client.get(
        "/admin/audit?action=alarm.ack&target_type=alarm",
        headers=_auth(hermes),
    ).get_json()
    assert any(
        entry["actor_id"] == "hermes"
        and entry["target_id"] == str(alarm_id)
        and entry["actor_type"] == "user"
        for entry in audit
    )


def test_agent_key_ack_is_attributed_to_agent():
    _reset_users()
    _reset_data()
    client = app.test_client()
    alarm_id = _new_alarm()

    ack = client.get(f"/alarms/{alarm_id}/ack", headers=AGENT_HEADERS)
    assert ack.status_code == 200

    detail = client.get(f"/alarms/{alarm_id}", headers=AGENT_HEADERS).get_json()
    assert detail["acknowledged_by"] == "testagent"
    assert detail["acknowledged_source"] == "agent"

    blocked = client.put("/admin/rules/r1", json={}, headers=AGENT_HEADERS)
    assert blocked.status_code == 403


def test_config_change_is_audited():
    _reset_users()
    _reset_data()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")

    rule = {
        "id": "r1",
        "title": "CPU test",
        "agentid": ["testagent"],
        "pluginid": "p1",
        "metric": "cpu",
        "condition": "gt",
        "threshold": 90,
        "scope": "single",
        "fire": "single",
        "severity": "warning",
    }
    saved = client.put("/admin/rules/r1", json=rule, headers=_auth(admin))
    assert saved.status_code == 200

    audit = client.get(
        "/admin/audit?action=config.rule.save&target_id=r1",
        headers=_auth(admin),
    ).get_json()
    assert audit
    assert audit[0]["actor_id"] == "daniel"
    assert audit[0]["target_type"] == "rule"


def test_plugin_config_audit_does_not_store_secrets():
    _reset_users()
    _reset_data()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")

    saved = client.put(
        "/admin/agents/testagent/plugins/vigor167",
        json={"host": "192.0.2.1", "password": "supersecret", "sleep": 30},
        headers=_auth(admin),
    )
    assert saved.status_code == 200

    audit = client.get(
        "/admin/audit?action=config.plugin.save&target_id=testagent%2Fvigor167",
        headers=_auth(admin),
    ).get_json()
    assert audit
    assert "supersecret" not in json.dumps(audit)


def test_password_change_invalidates_old_token():
    _reset_users()
    client = app.test_client()
    old = _login(client, "daniel", "secret")

    changed = client.put(
        "/account",
        json={"current_password": "secret", "new_password": "newsecret"},
        headers=_auth(old),
    )
    assert changed.status_code == 200
    new = changed.get_json()
    assert new["token"] != old["token"]

    rejected = client.get("/account", headers=_auth(old))
    assert rejected.status_code == 401
    accepted = client.get("/account", headers=_auth(new))
    assert accepted.status_code == 200


def test_cannot_demote_last_admin():
    _reset_users()
    client = app.test_client()
    admin = _login(client, "daniel", "secret")
    response = client.put(
        "/admin/users/daniel",
        json={"role": "user"},
        headers=_auth(admin),
    )
    assert response.status_code == 409


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
