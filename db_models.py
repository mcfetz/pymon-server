from datetime import datetime, UTC
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index,
    Integer, String, Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    endpoint = Column(Text, nullable=False, unique=True)
    p256dh_key = Column(String(255), nullable=False)
    auth_key = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)

class Metrics(Base):
    __tablename__ = "metrics"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    agentid = Column(String, nullable=False)
    pluginid = Column(String, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    received_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=True)
    metric = Column(String, nullable=False)
    value_float = Column(Float, nullable=True)
    # SQLite integers are 64-bit; PostgreSQL INTEGER is only 32-bit. Some
    # plugin counters exceed 2^31, so use BIGINT on both backends.
    value_int = Column(BigInteger, nullable=True)
    value_str = Column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_metrics_agent_plugin_metric_ts",
            "agentid",
            "pluginid",
            "metric",
            "timestamp",
        ),
        Index(
            "idx_metrics_agent_plugin_ts",
            "agentid",
            "pluginid",
            "timestamp",
        ),
        Index(
            "idx_metrics_plugin_metric",
            "pluginid",
            "metric",
        ),
        # Time-range queries and the default DESC ordering need their own index;
        # the other indexes all start with agent/plugin/metric and cannot serve
        # a query that only filters by timestamp.
        Index(
            "idx_metrics_timestamp",
            "timestamp",
        ),
    )


class MetricNote(Base):
    """One human annotation attached to a single stored metric value."""

    __tablename__ = "metric_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    metrics_id = Column(
        Integer,
        ForeignKey("metrics.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    note = Column(Text, nullable=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)


class Alarm(Base):
    __tablename__ = "alarms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agentid = Column(String, index=True, nullable=False)
    rule_id = Column(String, index=True, nullable=False)
    pluginid = Column(String, index=True, nullable=False)
    metric = Column(String, index=True, nullable=False)
    severity = Column(String, nullable=False)
    value = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    message = Column(Text, nullable=True)
    acknowledged = Column(Boolean, default=False)
    acknowledged_at = Column(DateTime, nullable=True)
    ack_method = Column(String, nullable=True)
    acknowledged_by = Column(String, nullable=True)
    acknowledged_source = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    note_updated_at = Column(DateTime, nullable=True)
    metrics_id = Column(Integer, ForeignKey("metrics.id"), nullable=False, index=True)

    __table_args__ = (
        Index(
            "idx_alarms_rule_ts",
            "rule_id",
            "created_at",
        ),
    )


class AuditLog(Base):
    """Append-only record of who changed what."""

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False, index=True)
    actor_type = Column(String, nullable=False, index=True)
    actor_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False, index=True)
    target_id = Column(String, nullable=True, index=True)
    summary = Column(Text, nullable=True)
    details = Column(Text, nullable=True)
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

    __table_args__ = (
        Index("idx_audit_target_ts", "target_type", "target_id", "created_at"),
    )


class MetricLastSeen(Base):
    """Tracks the most recent server receipt time per metric.

    Updated on every ingest (including discarded unchanged values) so no-data
    rules do not false-alarm when a metric keeps arriving but stops changing.
    """

    __tablename__ = "_metric_last_seen"

    agentid = Column(String, primary_key=True, nullable=False)
    pluginid = Column(String, primary_key=True, nullable=False)
    metric = Column(String, primary_key=True, nullable=False)
    last_received_at = Column(DateTime, nullable=True)
