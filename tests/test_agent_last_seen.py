"""agents.json must stay untouched on the metric ingest path.

Regression for the 14.11.2026 ENOSPC window on docker13: `touch_agent_last_seen()`
rewrote the whole 26 KB config (mkstemp + rename under the global config lock) after
every accepted POST — write amplification on the ingest hotpath, and when the
partition was full the write failed silently and `last_seen` was lost.

`last_seen` is now read from the ingest tracker `_metric_last_seen` (upserted on
every accepted ingest); `agents.json` is only written on admin changes.

Run with:  venv/bin/python tests/test_agent_last_seen.py
(No ssl, no network, no host access — everything in a temporary data dir.)
"""

import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime

TMP = tempfile.mkdtemp(prefix="pymon_agent_last_seen_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

from core import app, SessionLocal  # noqa: E402
from db_models import MetricLastSeen  # noqa: E402

import routes.admin  # noqa: E402,F401
import routes.metrics  # noqa: E402,F401

CONF = os.path.join(TMP, "conf")
AGENTS_JSON = os.path.join(CONF, "agents.json")

# A stale value an admin edit may have left behind in the config.
STALE_LAST_SEEN = "2026-01-01T00:00:00+00:00"

TEST_HEADERS = {"agentid": "testagent", "X-API-Key": "testkey"}
OLD_HEADERS = {"agentid": "oldagent", "X-API-Key": "oldkey"}


def _write_config():
    os.makedirs(CONF, exist_ok=True)
    config = {
        "agents": {
            "testagent": {
                "apikey": "testkey",
                "enabled": True,
                "plugins": {"p1": {"sleep": 60}},
            },
            "oldagent": {
                "apikey": "oldkey",
                "enabled": True,
                "last_seen": STALE_LAST_SEEN,
                "plugins": {"p1": {"sleep": 60}},
            },
        },
        "groups": {},
    }
    with open(AGENTS_JSON, "w", encoding="utf-8") as handle:
        json.dump(config, handle)


def _post_metrics(client, metrics=None, headers=None):
    payload = {
        "pluginid": "p1",
        "timestamp": datetime.now(UTC).isoformat(),
        "metrics": metrics if metrics is not None else [{"cpu": 1.0}],
    }
    return client.post("/metrics", json=payload, headers=headers or TEST_HEADERS)


def _config_state():
    with open(AGENTS_JSON, "rb") as handle:
        content = handle.read()
    stat = os.stat(AGENTS_JSON)
    return (content, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _db_last_seen(agentid):
    session = SessionLocal()
    try:
        return (
            session.query(func.max(MetricLastSeen.last_received_at))
            .filter(MetricLastSeen.agentid == agentid)
            .scalar()
        )
    finally:
        session.close()


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def test_ingest_does_not_rewrite_agents_json():
    _write_config()
    client = app.test_client()
    before = _config_state()

    response = _post_metrics(client)
    assert response.status_code == 200, response.get_json()

    after = _config_state()
    assert after[0] == before[0], "agents.json content changed during ingest"
    assert after[1] == before[1], "agents.json was replaced (new inode) during ingest"
    assert after[2] == before[2], "agents.json mtime changed during ingest"


def test_repeated_posts_leave_config_alone_and_track_in_db():
    _write_config()
    client = app.test_client()
    before = _config_state()

    for value in range(10):
        response = _post_metrics(client, metrics=[{"cpu": float(value)}])
        assert response.status_code == 200, response.get_json()

    after = _config_state()
    assert after[1:] == before[1:], (
        f"agents.json was rewritten: {before[1:]} -> {after[1:]}"
    )
    assert after[0] == before[0], "agents.json content changed during ingest"

    seen_at = _db_last_seen("testagent")
    assert seen_at is not None, "ingest did not update _metric_last_seen"


def test_admin_agents_last_seen_comes_from_db():
    _write_config()
    client = app.test_client()
    assert _post_metrics(client).status_code == 200

    db_seen = _db_last_seen("testagent")
    assert db_seen is not None

    listing = client.get("/admin/agents", headers=TEST_HEADERS)
    assert listing.status_code == 200, listing.get_json()
    data = listing.get_json()["testagent"]

    reported = _aware(datetime.fromisoformat(data["last_seen"]))
    assert reported == _aware(db_seen), (
        f"last_seen {reported} != ingest tracker {_aware(db_seen)}"
    )
    assert data["online"] is True


def test_agent_without_metrics_falls_back_to_config_value():
    _write_config()
    client = app.test_client()

    listing = client.get("/admin/agents", headers=OLD_HEADERS)
    assert listing.status_code == 200, listing.get_json()
    data = listing.get_json()["oldagent"]

    reported = _aware(datetime.fromisoformat(data["last_seen"]))
    assert reported == _aware(datetime.fromisoformat(STALE_LAST_SEEN))
    assert data["online"] is False


class _ErrorCatcher(logging.Handler):
    """Collect ERROR-level log records so a swallowed write failure is visible."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_ingest_survives_unwritable_config_dir():
    """A full/read-only config partition must not cost us last_seen any more."""
    if os.geteuid() == 0:
        print("  SKIP  running as root: directory permissions are not enforced")
        return

    _write_config()
    client = app.test_client()
    before = _db_last_seen("testagent")

    catcher = _ErrorCatcher()
    root_logger = logging.getLogger()
    root_logger.addHandler(catcher)
    try:
        os.chmod(CONF, 0o555)
        try:
            response = _post_metrics(client, metrics=[{"cpu": 7.0}])
        finally:
            os.chmod(CONF, 0o755)
    finally:
        root_logger.removeHandler(catcher)

    assert response.status_code == 200, response.get_json()
    after = _db_last_seen("testagent")
    assert after is not None
    if before is not None:
        assert _aware(after) >= _aware(before)

    problems = []
    for record in catcher.records:
        message = record.getMessage()
        if "last_seen" in message or (
            record.exc_info and "Errno 13" in str(record.exc_info[1])
        ):
            problems.append(message)
    assert not problems, f"the ingest still tried to write the config: {problems}"


def main():
    print("agent last_seen / agents.json tests")
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
