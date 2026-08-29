"""Automatic daily metric cleanup job.

Runs once per day at the configured local server time and deletes metrics
older than the configured retention period in small batches, releasing the
DB write lock between batches so agent ingest is never blocked for long.

Last run statistics (start, duration, deleted rows) are persisted so the UI
can show them even after a restart.
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

from config import CONF_DIR
from core import DB_WRITE_LOCK, SessionLocal, logger
from db_models import Alarm, Metrics

CLEANUP_JSON = os.path.join(CONF_DIR, "cleanup_job.json")
CHECK_INTERVAL_SECONDS = 30
BATCH_SLEEP_SECONDS = 1.0

_lock = threading.Lock()

_DEFAULTS = {
    "enabled": False,
    "time": "03:00",
    "retention_days": 30,
    "batch_size": 500,
    "last_run": None,
    "last_run_date": None,
}


def _save(data: dict) -> None:
    os.makedirs(CONF_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONF_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CLEANUP_JSON)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load() -> dict:
    data = dict(_DEFAULTS)
    if os.path.exists(CLEANUP_JSON):
        try:
            with open(CLEANUP_JSON, encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Corrupt cleanup job config %s: %s", CLEANUP_JSON, e)
    return data


def _parse_time(value) -> tuple[int, int] | None:
    try:
        hour_str, _, minute_str = str(value).strip().partition(":")
        hour = int(hour_str)
        minute = int(minute_str)
    except (ValueError, TypeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _should_run(data: dict) -> bool:
    if not data.get("enabled"):
        return False
    parsed = _parse_time(data.get("time", "03:00"))
    if parsed is None:
        return False
    now = datetime.now()
    if (now.hour, now.minute) < parsed:
        return False
    today = now.date().isoformat()
    return data.get("last_run_date") != today


def _cleanup(retention_days: int, batch_size: int) -> tuple[int | None, int | None]:
    """Delete metrics older than the cutoff in small batches.

    Returns (deleted_metrics, deleted_alarms) or (None, None) on failure.
    """
    cutoff = datetime.now() - timedelta(days=retention_days)
    filters = [Metrics.timestamp < cutoff]
    session = SessionLocal()
    deleted_metrics = 0
    deleted_alarms = 0
    try:
        from sqlalchemy import select
        with DB_WRITE_LOCK:
            metric_ids = select(Metrics.id).where(*filters)
            deleted_alarms = (
                session.query(Alarm)
                .filter(Alarm.metrics_id.in_(metric_ids))
                .delete(synchronize_session=False)
            )
            session.commit()
        while True:
            with DB_WRITE_LOCK:
                batch_ids = [
                    row[0]
                    for row in session.query(Metrics.id)
                    .filter(*filters)
                    .limit(batch_size)
                    .all()
                ]
                if not batch_ids:
                    break
                deleted_metrics += (
                    session.query(Metrics)
                    .filter(Metrics.id.in_(batch_ids))
                    .delete(synchronize_session=False)
                )
                session.commit()
            if len(batch_ids) < batch_size:
                break
            # Low-load: pause between batches so ingest is never starved.
            time.sleep(BATCH_SLEEP_SECONDS)
    except Exception:
        session.rollback()
        logger.error("Cleanup job run failed", exc_info=True)
        return None, None
    finally:
        session.close()
    return deleted_metrics, deleted_alarms


def get_state() -> dict:
    """Public state for the UI: settings, last run and next run."""
    with _lock:
        data = _load()
    return {
        "enabled": bool(data.get("enabled")),
        "time": str(data.get("time", "03:00")),
        "retention_days": int(data.get("retention_days", 30) or 30),
        "batch_size": int(data.get("batch_size", 500) or 500),
        "last_run": data.get("last_run"),
        "next_run": _compute_next_run(data),
    }


def _compute_next_run(data: dict) -> str | None:
    if not data.get("enabled"):
        return None
    parsed = _parse_time(data.get("time", "03:00"))
    if parsed is None:
        return None
    now = datetime.now()
    scheduled = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    today = now.date().isoformat()
    if now < scheduled:
        return scheduled.isoformat()
    if data.get("last_run_date") != today:
        return scheduled.isoformat()
    return (scheduled + timedelta(days=1)).isoformat()


def apply_settings(payload: dict) -> dict | tuple[dict, int]:
    """Validate and persist settings from the UI."""
    parsed_time = _parse_time(payload.get("time", "03:00"))
    if parsed_time is None:
        return {"error": "time must be in HH:MM format (24h)"}, 400
    try:
        retention_days = int(payload.get("retention_days", 30))
    except (TypeError, ValueError):
        return {"error": "retention_days must be an integer"}, 400
    if retention_days < 1:
        return {"error": "retention_days must be >= 1"}, 400
    try:
        batch_size = int(payload.get("batch_size", 500))
    except (TypeError, ValueError):
        return {"error": "batch_size must be an integer"}, 400
    if batch_size < 100 or batch_size > 5000:
        return {"error": "batch_size must be between 100 and 5000"}, 400

    with _lock:
        data = _load()
        data["enabled"] = bool(payload.get("enabled"))
        data["time"] = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"
        data["retention_days"] = retention_days
        data["batch_size"] = batch_size
        _save(data)
    return get_state()


def run_once() -> dict | None:
    """Scheduled entry point; returns the run result or None if not due."""
    with _lock:
        data = _load()
        if not _should_run(data):
            return None
        retention_days = int(data.get("retention_days", 30) or 30)
        batch_size = max(100, min(int(data.get("batch_size", 500) or 500), 5000))
        started = datetime.now()
        data["last_run_date"] = started.date().isoformat()
        _save(data)

    deleted_metrics, deleted_alarms = _cleanup(retention_days, batch_size)
    finished = datetime.now()
    result = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "deleted_metrics": deleted_metrics,
        "deleted_alarms": deleted_alarms,
        "retention_days": retention_days,
        "status": "completed" if deleted_metrics is not None else "failed",
    }
    logger.info("Cleanup job %s", result)
    with _lock:
        data = _load()
        data["last_run"] = result
        _save(data)
    return result


def _loop() -> None:
    while True:
        try:
            run_once()
        except Exception:
            logger.error("Cleanup job iteration failed", exc_info=True)
        threading.Event().wait(CHECK_INTERVAL_SECONDS)


def start_cleanup_job() -> threading.Thread:
    """Start the daemon cleanup scheduler thread."""
    thread = threading.Thread(
        target=_loop,
        name="pymon-cleanup-job",
        daemon=True,
    )
    thread.start()
    return thread