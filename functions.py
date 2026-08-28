from db_models import Metrics
from datetime import datetime
from dateutil import parser as dateutil_parser


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
