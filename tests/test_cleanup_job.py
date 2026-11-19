"""Tests for the daily metric cleanup job.

Two things matter here and both regressed in production before:

1. The batches really delete everything older than the cutoff (and keep
   recent rows plus their alarms).
2. On PostgreSQL the cleanup must NOT take the process-wide ``DB_WRITE_LOCK``.
   It used to hold that lock for the whole run (~14 min nightly, ~2 650
   batches), which serialized every agent ingest behind it: on 2026-11-19 the
   ingest latency went from p50 0.4 s to p50 14 s / p90 32 s inside the
   02:00 UTC window, so the agents hit their 15 s read timeout and the
   swarm agent killed itself via its own watchdog (exit 3).
"""

import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta

TMP = tempfile.mkdtemp(prefix="pymon_cleanup_job_")
os.environ["PYMON_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cleanup_job  # noqa: E402
from core import SessionLocal  # noqa: E402
from db_models import Alarm, MetricNote, Metrics  # noqa: E402


class RecordingLock:
    """Stand-in for ``DB_WRITE_LOCK`` that counts acquisitions."""

    def __init__(self):
        self.acquired = 0

    def __enter__(self):
        self.acquired += 1
        return self

    def __exit__(self, *exc_info):
        return False


def _reset_data():
    session = SessionLocal()
    try:
        session.query(MetricNote).delete()
        session.query(Alarm).delete()
        session.query(Metrics).delete()
        session.commit()
    finally:
        session.close()


def _add_metrics(count=0, age_days=30):
    """Insert ``count`` metrics ``age_days`` old; return their ids."""
    session = SessionLocal()
    try:
        rows = [
            Metrics(
                agentid="a1",
                pluginid="p1",
                metric="cpu",
                timestamp=datetime.now(UTC) - timedelta(days=age_days),
                value_float=1.0,
            )
            for _ in range(count)
        ]
        session.add_all(rows)
        session.commit()
        return [row.id for row in rows]
    finally:
        session.close()


def _add_alarm(metrics_id):
    session = SessionLocal()
    try:
        alarm = Alarm(
            agentid="a1",
            rule_id="r1",
            pluginid="p1",
            metric="cpu",
            severity="warning",
            message="test alarm",
            acknowledged=False,
            metrics_id=metrics_id,
        )
        session.add(alarm)
        session.commit()
        return alarm.id
    finally:
        session.close()


def _add_note(metrics_id, note="old note"):
    session = SessionLocal()
    try:
        entry = MetricNote(metrics_id=metrics_id, note=note, created_by="tester")
        session.add(entry)
        session.commit()
    finally:
        session.close()


def _run_with(fake_lock, postgres):
    """Run the cleanup once with a fake lock and a forced backend flavour."""
    original_lock = cleanup_job.DB_WRITE_LOCK
    original_pg = cleanup_job.IS_POSTGRES
    cleanup_job.DB_WRITE_LOCK = fake_lock
    cleanup_job.IS_POSTGRES = postgres
    try:
        return cleanup_job._cleanup(retention_days=21, batch_size=100)
    finally:
        cleanup_job.DB_WRITE_LOCK = original_lock
        cleanup_job.IS_POSTGRES = original_pg


def test_cleanup_deletes_everything_older_than_cutoff_in_batches():
    _reset_data()
    _add_metrics(count=250, age_days=30)
    recent = _add_metrics(count=1, age_days=0)

    deleted_metrics, deleted_alarms = _run_with(cleanup_job.DB_WRITE_LOCK, False)

    assert deleted_metrics == 250, deleted_metrics
    assert deleted_alarms == 0, deleted_alarms
    session = SessionLocal()
    try:
        assert session.query(Metrics).count() == 1
        assert session.query(Metrics.id).filter(Metrics.id == recent[0]).count() == 1
    finally:
        session.close()


def test_cleanup_removes_alarms_of_deleted_metrics_only():
    _reset_data()
    old = _add_metrics(count=1, age_days=30)
    recent = _add_metrics(count=1, age_days=0)
    old_alarm = _add_alarm(old[0])
    recent_alarm = _add_alarm(recent[0])

    _run_with(cleanup_job.DB_WRITE_LOCK, False)

    session = SessionLocal()
    try:
        remaining = {row.id for row in session.query(Alarm.id).all()}
    finally:
        session.close()
    assert old_alarm not in remaining
    assert recent_alarm in remaining


def test_cleanup_removes_notes_of_deleted_metrics_only():
    _reset_data()
    old = _add_metrics(count=1, age_days=30)
    recent = _add_metrics(count=1, age_days=0)
    _add_note(old[0], "old note")
    _add_note(recent[0], "recent note")

    _run_with(cleanup_job.DB_WRITE_LOCK, False)

    session = SessionLocal()
    try:
        remaining = [row.note for row in session.query(MetricNote.note).all()]
    finally:
        session.close()
    assert remaining == ["recent note"], remaining


def test_cleanup_skips_global_write_lock_on_postgres():
    _reset_data()
    _add_metrics(count=250, age_days=30)
    lock = RecordingLock()

    deleted_metrics, _deleted_alarms = _run_with(lock, True)

    assert deleted_metrics == 250, deleted_metrics
    assert lock.acquired == 0, (
        f"cleanup took the global write lock {lock.acquired}x on PostgreSQL"
    )


def test_cleanup_still_serializes_writers_on_sqlite():
    _reset_data()
    _add_metrics(count=250, age_days=30)
    lock = RecordingLock()

    deleted_metrics, _deleted_alarms = _run_with(lock, False)

    assert deleted_metrics == 250, deleted_metrics
    # One acquisition for the note/alarm pass plus one per metrics batch
    # (250 rows / batch_size 100 = 3 batches).
    assert lock.acquired == 4, lock.acquired


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
