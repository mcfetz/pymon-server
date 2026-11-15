"""Liveness of pluginid=agent/metric=online must reach the no-data rule engine.

Regression for rule `rem5anzz` ("agent offline"), which never fired: the agent
only calls /agents/status on start and stop, and that path wrote the metric into
`metrics` without touching `_metric_last_seen` — the only table no-data rules
read. A killed agent (SIGKILL/OOM/firmware update) writes no `online=0` at all,
so the outage stayed invisible.

Fix: the status endpoint refreshes the tracker for online/version, and every
accepted metric push refreshes the agent-level liveness row as well (any push
proves the agent is alive, independent of the plugin cadence).

Run with:  venv/bin/python tests/test_agent_liveness.py
(No ssl, no network, no host access — everything in a temporary data dir.)
"""

import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_agent_liveness_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

from core import SessionLocal, app  # noqa: E402
from db_models import Alarm, MetricLastSeen  # noqa: E402
from no_data_monitor import evaluate_no_data_rules  # noqa: E402

import routes.admin  # noqa: E402,F401
import routes.agents  # noqa: E402,F401
import routes.metrics  # noqa: E402,F401

CONF = os.path.join(TMP, "conf")
AGENTS_JSON = os.path.join(CONF, "agents.json")
RULES_JSON = os.path.join(CONF, "rules.json")

TEST_HEADERS = {"agentid": "testagent", "X-API-Key": "testkey"}
RULE_ID = "rem5anzz"
THRESHOLD_SECONDS = 900

logger = logging.getLogger("pymon_agent_liveness")
logger.setLevel(logging.WARNING)


def _write_config() -> None:
    os.makedirs(CONF, exist_ok=True)
    config = {
        "agents": {
            "testagent": {
                "apikey": "testkey",
                "enabled": True,
                "title": "Test agent",
                "plugins": {"p1": {"sleep": 60}},
            }
        },
        "groups": {},
    }
    with open(AGENTS_JSON, "w", encoding="utf-8") as handle:
        json.dump(config, handle)


def _write_rules() -> None:
    """The live shape of the rebuilt rule: no_data on the agent liveness metric."""
    os.makedirs(CONF, exist_ok=True)
    rules = {
        RULE_ID: {
            "enabled": True,
            "title": "agent offline",
            "description": "no liveness signal from the agent",
            "pluginid": "agent",
            "metric": "online",
            "condition": "no_data",
            "threshold": str(THRESHOLD_SECONDS),
            "scope": "single",
            "severity": "critical",
            "fire": "single",
            "auto_close": True,
            "notifications": [],
            "executors": [],
            "agents": [],
            "agents_mode": "exclude",
        }
    }
    with open(RULES_JSON, "w", encoding="utf-8") as handle:
        json.dump(rules, handle)


def _last_seen(metric: str = "online") -> datetime | None:
    session = SessionLocal()
    try:
        return (
            session.query(func.max(MetricLastSeen.last_received_at))
            .filter(
                MetricLastSeen.agentid == "testagent",
                MetricLastSeen.pluginid == "agent",
                MetricLastSeen.metric == metric,
            )
            .scalar()
        )
    finally:
        session.close()


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _post_status(client, status: str, version: str = "deadbeef"):
    return client.post(
        "/agents/status",
        json={"status": status, "version": version},
        headers=TEST_HEADERS,
    )


def _post_metrics(client):
    payload = {
        "pluginid": "p1",
        "timestamp": datetime.now(UTC).isoformat(),
        "metrics": [{"cpu": 1.0}],
    }
    return client.post("/metrics", json=payload, headers=TEST_HEADERS)


def _open_alarms(rule_id: str = RULE_ID) -> list[Alarm]:
    session = SessionLocal()
    try:
        return (
            session.query(Alarm)
            .filter(Alarm.rule_id == rule_id, Alarm.acknowledged == False)  # noqa: E712
            .all()
        )
    finally:
        session.close()


def test_status_online_tracks_liveness():
    _write_config()
    client = app.test_client()

    response = _post_status(client, "online")
    assert response.status_code == 200, response.get_json()

    seen = _aware(_last_seen("online"))
    assert seen is not None, "status=online did not create the agent liveness row"
    assert abs((datetime.now(UTC) - seen).total_seconds()) < 5, seen

    version = _aware(_last_seen("version"))
    assert version is not None, "status endpoint did not track agent:version"


def test_status_offline_does_not_refresh_liveness():
    _write_config()
    client = app.test_client()

    assert _post_status(client, "online").status_code == 200
    before = _aware(_last_seen("online"))
    assert before is not None

    assert _post_status(client, "offline").status_code == 200
    after = _aware(_last_seen("online"))
    assert after == before, (
        "a clean shutdown must not refresh liveness, otherwise the no-data "
        f"alarm is postponed by the whole threshold: {before} -> {after}"
    )


def test_metric_push_refreshes_liveness():
    _write_config()
    client = app.test_client()

    assert _post_status(client, "online").status_code == 200
    before = _aware(_last_seen("online"))
    assert before is not None

    assert _post_metrics(client).status_code == 200

    after = _aware(_last_seen("online"))
    assert after is not None
    assert after > before, (
        f"a metric push did not refresh the liveness row: {before} -> {after}"
    )


def test_no_data_rule_fires_on_stale_liveness():
    _write_config()
    _write_rules()
    client = app.test_client()

    assert _post_status(client, "online").status_code == 200
    seen = _aware(_last_seen("online"))
    assert seen is not None

    # Still inside the window: no alarm.
    session = SessionLocal()
    try:
        actions = evaluate_no_data_rules(
            session, now=seen + timedelta(seconds=THRESHOLD_SECONDS - 60)
        )
        session.commit()
    finally:
        session.close()
    assert actions == []
    assert _open_alarms() == []

    # Past the window: exactly one alarm for the agent liveness rule.
    session = SessionLocal()
    try:
        evaluate_no_data_rules(
            session, now=seen + timedelta(seconds=THRESHOLD_SECONDS + 60)
        )
        session.commit()
    finally:
        session.close()

    alarms = _open_alarms()
    assert len(alarms) == 1, f"expected one open alarm, got {alarms}"
    alarm = alarms[0]
    assert alarm.agentid == "testagent"
    assert alarm.pluginid == "agent"
    assert alarm.metric == "online"
    assert alarm.severity == "critical"

    # fire=single: a second pass must not duplicate the alarm.
    session = SessionLocal()
    try:
        evaluate_no_data_rules(
            session, now=seen + timedelta(seconds=THRESHOLD_SECONDS + 120)
        )
        session.commit()
    finally:
        session.close()
    assert len(_open_alarms()) == 1


def test_no_data_alarm_auto_closes_when_data_returns():
    _write_config()
    _write_rules()
    client = app.test_client()

    assert _post_status(client, "online").status_code == 200
    seen = _aware(_last_seen("online"))
    assert seen is not None

    session = SessionLocal()
    try:
        evaluate_no_data_rules(
            session, now=seen + timedelta(seconds=THRESHOLD_SECONDS + 60)
        )
        session.commit()
    finally:
        session.close()
    assert len(_open_alarms()) == 1

    # Data is flowing again: auto_close acknowledges the alarm.
    assert _post_metrics(client).status_code == 200
    session = SessionLocal()
    try:
        evaluate_no_data_rules(session, now=datetime.now(UTC))
        session.commit()
    finally:
        session.close()
    assert _open_alarms() == [], (
        "auto_close did not acknowledge the alarm after recovery"
    )


def main():
    print("agent liveness / no-data rule tests")
    # `rules.load_rules` caches its parse for 5 s, so the rule file has to exist
    # before the first request in this process (every test takes well under 5 s,
    # so a later write would not be picked up).
    _write_config()
    _write_rules()
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
