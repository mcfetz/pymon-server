from sqlalchemy import text

from db_models import Metrics
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
