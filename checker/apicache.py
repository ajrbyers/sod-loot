"""Tiny cache helper that also reports hit/miss and age, so the UI can show
"cached 12m ago" and offer a force-refresh."""
import time

from django.conf import settings
from django.core.cache import cache


def get_or_set(key, producer, force=False, ttl=None):
    """Return (data, meta). meta = {cached: bool, age: seconds, ttl: seconds}.

    force=True bypasses any cached value and refreshes it.
    """
    ttl = settings.API_CACHE_SECONDS if ttl is None else ttl
    if not force:
        wrapped = cache.get(key)
        if wrapped is not None:
            return wrapped["data"], {
                "cached": True,
                "age": int(time.time() - wrapped["at"]),
                "ttl": ttl,
            }
    data = producer()
    cache.set(key, {"data": data, "at": time.time()}, ttl)
    return data, {"cached": False, "age": 0, "ttl": ttl}


def summarise(metas):
    """Combine several call metas into one summary for the response."""
    metas = [m for m in metas if m]
    if not metas:
        return {"cached": False, "all_cached": False, "age_seconds": 0}
    return {
        "cached": any(m["cached"] for m in metas),
        "all_cached": all(m["cached"] for m in metas),
        "age_seconds": max(m["age"] for m in metas),
        "ttl_seconds": max(m["ttl"] for m in metas),
    }
