"""Background evaluation for rules that detect missing metric data."""

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import func

from core import DB_WRITE_LOCK, SessionLocal
from db_models import MetricLastSeen, Metrics
from rules import (
    PostCommitAction,
    _ack_open_alarms,
    _maybe_create_alarm,
    _resolve_threshold,
    has_open_alarm,
    load_rules,
    _rule_applies_to_agent,
)

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30


def _latest_metrics_for_rule(session, rule) -> list[dict]:
    """Return one latest received metric per agent for an exact rule metric.

    Uses the per-metric last-received tracker, which is updated on every ingest
    (including discarded unchanged values), so no-data rules keep working when
    the discard feature suppresses writes for stable metrics.
    """
    rows = (
        session.query(
            MetricLastSeen.agentid,
            func.max(MetricLastSeen.last_received_at).label("last_received_at"),
        )
        .where(
            MetricLastSeen.pluginid == rule.pluginid,
            MetricLastSeen.metric == rule.metric,
            MetricLastSeen.last_received_at.is_not(None),
        )
        .group_by(MetricLastSeen.agentid)
        .all()
    )

    result = []
    for agentid, last_received_at in rows:
        # Alarm.metrics_id must reference a persisted row; every metric has at
        # least one stored value (the first poll is never discarded).
        metric_id = (
            session.query(func.max(Metrics.id))
            .where(
                Metrics.agentid == agentid,
                Metrics.pluginid == rule.pluginid,
                Metrics.metric == rule.metric,
            )
            .scalar()
        )
        result.append(
            {
                "agentid": agentid,
                "last_received_at": last_received_at,
                "metric_id": metric_id,
            }
        )
    return result


def evaluate_no_data_rules(
    session,
    now: datetime | None = None,
) -> list[PostCommitAction]:
    """Evaluate enabled no-data rules and return post-commit actions."""
    checked_at = now or datetime.now(UTC)
    actions: list[PostCommitAction] = []

    for rule in load_rules():
        if not rule.enabled or rule.condition != "no_data" or rule.scope != "single":
            continue
        if not rule.pluginid or rule.pluginid == "*":
            logger.warning("rule '%s': no-data not supported for wildcard plugin selector", rule.id)
            continue

        for latest_metric in _latest_metrics_for_rule(session, rule):
            agentid = latest_metric["agentid"]
            if not _rule_applies_to_agent(rule, agentid):
                continue
            received_at = latest_metric["last_received_at"]
            if received_at is None:
                continue
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
            metric_id = latest_metric["metric_id"]
            if metric_id is None:
                continue

            try:
                threshold_seconds = _resolve_threshold(rule.threshold, agentid)
            except (TypeError, ValueError):
                logger.warning("rule '%s' has an invalid no-data threshold", rule.id)
                continue
            if threshold_seconds <= 0:
                logger.warning("rule '%s' has a non-positive no-data threshold", rule.id)
                continue

            elapsed_seconds = max(0.0, (checked_at - received_at).total_seconds())
            if elapsed_seconds >= threshold_seconds:
                # No-data alarms stay single-open even if fire=multi is selected.
                if not has_open_alarm(session, agentid, rule):
                    _maybe_create_alarm(
                        session,
                        agentid,
                        rule,
                        rule.metric,
                        elapsed_seconds,
                        metric_id,
                        actions,
                    )
            elif rule.auto_close:
                _ack_open_alarms(session, agentid, rule, rule.metric)

    return actions


def check_no_data_once() -> None:
    """Run one no-data pass, committing alarms before external actions."""
    session = SessionLocal()
    actions: list[PostCommitAction] = []
    try:
        with DB_WRITE_LOCK:
            actions = evaluate_no_data_rules(session)
            session.commit()
    except Exception:
        with DB_WRITE_LOCK:
            session.rollback()
        logger.error("No-data rule evaluation failed", exc_info=True)
        return
    finally:
        session.close()

    for action in actions:
        try:
            action()
        except Exception:
            logger.error("No-data post-commit action failed", exc_info=True)


def _monitor_loop() -> None:
    while True:
        try:
            check_no_data_once()
        except Exception:
            logger.error("No-data monitor iteration failed", exc_info=True)
        threading.Event().wait(CHECK_INTERVAL_SECONDS)


def start_no_data_monitor() -> threading.Thread:
    """Start the daemon monitor thread used by the standalone server."""
    thread = threading.Thread(
        target=_monitor_loop,
        name="pymon-no-data-monitor",
        daemon=True,
    )
    thread.start()
    return thread
