"""Background evaluation for rules that detect missing metric data."""

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import and_, func, select

from core import DB_WRITE_LOCK, SessionLocal
from db_models import Metrics
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


def _latest_metrics_for_rule(session, rule) -> list[Metrics]:
    """Return one latest received metric row per agent for an exact rule metric."""
    latest = (
        select(
            Metrics.agentid,
            func.max(Metrics.received_at).label("last_received_at"),
        )
        .where(
            Metrics.pluginid == rule.pluginid,
            Metrics.metric == rule.metric,
            Metrics.received_at.is_not(None),
        )
        .group_by(Metrics.agentid)
        .subquery()
    )
    rows = (
        session.query(Metrics)
        .join(
            latest,
            and_(
                Metrics.agentid == latest.c.agentid,
                Metrics.received_at == latest.c.last_received_at,
                Metrics.pluginid == rule.pluginid,
                Metrics.metric == rule.metric,
            ),
        )
        .all()
    )

    # A timestamp collision can produce more than one row; keep the newest ID.
    latest_by_agent: dict[str, Metrics] = {}
    for row in rows:
        current = latest_by_agent.get(row.agentid)
        if current is None or row.id > current.id:
            latest_by_agent[row.agentid] = row
    return list(latest_by_agent.values())


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

        for latest_metric in _latest_metrics_for_rule(session, rule):
            if not _rule_applies_to_agent(rule, latest_metric.agentid):
                continue
            received_at = latest_metric.received_at
            if received_at is None:
                continue
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)

            try:
                threshold_seconds = _resolve_threshold(rule.threshold, latest_metric.agentid)
            except (TypeError, ValueError):
                logger.warning("rule '%s' has an invalid no-data threshold", rule.id)
                continue
            if threshold_seconds <= 0:
                logger.warning("rule '%s' has a non-positive no-data threshold", rule.id)
                continue

            elapsed_seconds = max(0.0, (checked_at - received_at).total_seconds())
            if elapsed_seconds >= threshold_seconds:
                # No-data alarms stay single-open even if fire=multi is selected.
                if not has_open_alarm(session, latest_metric.agentid, rule):
                    _maybe_create_alarm(
                        session,
                        latest_metric.agentid,
                        rule,
                        rule.metric,
                        elapsed_seconds,
                        latest_metric.id,
                        actions,
                    )
            elif rule.auto_close:
                _ack_open_alarms(session, latest_metric.agentid, rule, rule.metric)

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
