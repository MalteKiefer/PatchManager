"""Fixed window counters in the shared cache."""

from django.core.cache import cache


def hit(key: str, *, limit: int, window: int) -> bool:
    """Count one event; return ``False`` once ``limit`` is exceeded in ``window`` seconds."""
    cache_key = f"rl:{key}"
    if cache.add(cache_key, 1, timeout=window):
        return True
    try:
        return cache.incr(cache_key) <= limit
    except ValueError:  # expired between add and incr
        cache.add(cache_key, 1, timeout=window)
        return True


def reset(key: str) -> None:
    """Forget a counter, for example after a successful login."""
    cache.delete(f"rl:{key}")
