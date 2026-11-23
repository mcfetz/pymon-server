from sqlalchemy import func, text

from db_models import MetricLastSeen, Metrics
from datetime import datetime
from dateutil import parser as dateutil_parser


def touch_metric_last_seen(
    session, agentid: str, pluginid: str, metric: str, received_at
) -> None:
    """Upsert the ingest tracker row for one (agentid, pluginid, metric) triple.

    no-data rules read exclusively ``_metric_last_seen``, so every code path that
    accepts data for a metric has to refresh its row — the push ingest and the
    agent status endpoint alike. Without this the metric exists in ``metrics``
    but the rule engine never sees it.
    """
    session.execute(
        text(
            "INSERT INTO _metric_last_seen "
            "(agentid, pluginid, metric, last_received_at) "
            "VALUES (:agentid, :pluginid, :metric, :last_received_at) "
            "ON CONFLICT (agentid, pluginid, metric) "
            "DO UPDATE SET last_received_at = EXCLUDED.last_received_at"
        ),
        {
            "agentid": agentid,
            "pluginid": pluginid,
            "metric": metric,
            "last_received_at": received_at,
        },
    )


def list_metric_names(
    session, agentid: str | None = None, pluginid: str | None = None
) -> list[str]:
    """Distinct metric names of the requested scope, read from the ingest tracker.

    The name of a metric is an attribute of its time series, not of every row, so
    asking ``metrics`` for the names costs a DISTINCT over the whole retention
    window. Measured against the live DB (2026-11-23, 24.5M rows / 13 GB):
    1953 ms and 272,875 index entries for one agent+plugin pair, 437 ms for a
    plugin across all agents, and ~860 dirty pages written back per call. The
    tracker ``_metric_last_seen`` has exactly one row per (agentid, pluginid,
    metric) and is refreshed on every accepted ingest, so the same list comes out
    in ~20 ms without touching ``metrics`` at all.

    The tracker is never pruned while ``metrics`` is (daily cleanup job), so it
    also holds names whose rows were deleted long ago: 44,779 tracker rows vs
    3,595 names for the busiest agent+plugin. The list is therefore limited to
    rows at or after the oldest surviving row of ``metrics`` — the cutoff the
    cleanup job actually used, read from the data instead of its config so it
    stays correct when the retention or the job changes. Verified live: for the
    six busiest pairs the window lists 3,581/3,595, 1,543/1,547, 1,524/1,529,
    605/605, 395/395 and 1,556/1,561 of the names the DISTINCT over ``metrics``
    returns, and no name without data.

    Falls back to the DISTINCT over ``metrics`` when the tracker has no row at
    all for the requested scope (a metric ingested before the tracker existed, or
    a row written by a path that does not touch it).
    """
    cutoff = session.query(func.min(Metrics.timestamp)).scalar()

    query = session.query(MetricLastSeen.metric)
    if agentid is not None:
        query = query.filter(MetricLastSeen.agentid == agentid)
    if pluginid is not None:
        query = query.filter(MetricLastSeen.pluginid == pluginid)
    if cutoff is not None:
        query = query.filter(MetricLastSeen.last_received_at >= cutoff)

    names = {name for (name,) in query.distinct().all() if name is not None}
    if names:
        return sorted(names)

    fallback = session.query(Metrics.metric)
    if agentid is not None:
        fallback = fallback.filter(Metrics.agentid == agentid)
    if pluginid is not None:
        fallback = fallback.filter(Metrics.pluginid == pluginid)
    return sorted({name for (name,) in fallback.distinct().all() if name is not None})


def _parse_time_param(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if " " in value and ("+" not in value and "-" not in value[10:]):
        parts = value.rsplit(" ", 1)
        if len(parts) == 2 and ":" in parts[1]:
            value = f"{parts[0]}+{parts[1]}"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return dateutil_parser.parse(value)
    except Exception:
        return None


def dict_value_to_metric(value, metric: Metrics):
    value_float = value_int = value_str = None
    # bool is a subclass of int, so it must be handled first. PostgreSQL will
    # not implicitly cast a boolean parameter into the BIGINT value_int column.
    if isinstance(value, bool):
        value_int = 1 if value else 0
    elif isinstance(value, float):
        value_float = value
    elif isinstance(value, int):
        value_int = value
    elif isinstance(value, str):
        value_str = value
    elif value:
        value_str = str(value)

    metric.value_float = value_float
    metric.value_int = value_int
    metric.value_str = value_str

    return metric


def get_value_from_row(row):
    # Use "is not None" to handle 0, 0.0, and "" as valid values
    if row.value_int is not None:
        return row.value_int
    if row.value_float is not None:
        return row.value_float
    return row.value_str
