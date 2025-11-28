from datetime import datetime, UTC
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Index, Boolean
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Metrics(Base):
    __tablename__ = "metrics"

    id = Column(Integer, primary_key=True, index=True)
    agentid = Column(String, nullable=False)
    pluginid = Column(String, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    metric = Column(String, nullable=False)
    value_float = Column(Float, nullable=True)
    value_int = Column(Integer, nullable=True)
    value_str = Column(Text, nullable=True)

    __table_args__ = (
        Index(
            "idx_metrics_agent_plugin_metric_ts",
            "agentid",
            "pluginid",
            "metric",
            "timestamp",
        ),
    )


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
