import time
from functools import wraps
from typing import Any


def timed_cache(ttl_seconds: int):
    def decorator(func):
        cache_data: dict[str, tuple[Any, float]] = {}

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = str(args) + str(sorted(kwargs.items()))
            now = time.time()
            if key in cache_data:
                value, timestamp = cache_data[key]
                if (now - timestamp) < ttl_seconds:
                    return value
            result = func(*args, **kwargs)
            cache_data[key] = (result, now)
            return result

        return wrapper

    return decorator
