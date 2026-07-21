from db_models import Metrics
from datetime import datetime


def _parse_time_param(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def dict_value_to_metric(value, metric: Metrics):
    value_float = value_int = value_str = None
    if isinstance(value, float):
        value_float = value
    elif isinstance(value, int):
        value_int = value
    elif isinstance(value, str):
        value_str = value
    elif isinstance(value, bool):
        value_int = 1 if value else 0
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
