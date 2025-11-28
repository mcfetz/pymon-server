import time
from functools import wraps


def timed_cache(ttl_seconds: int):
    def decorator(func):
        cache_data = {"value": None, "timestamp": 0.0}

        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            if cache_data["value"] is not None and (now - cache_data["timestamp"]) < ttl_seconds:
                return cache_data["value"]
            result = func(*args, **kwargs)
            cache_data["value"] = result
            cache_data["timestamp"] = now
            return result

        return wrapper

    return decorator
