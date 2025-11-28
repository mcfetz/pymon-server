from db_models import Metrics


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
