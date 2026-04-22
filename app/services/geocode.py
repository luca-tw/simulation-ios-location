import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "simulation-ios-location/1.0 (self-hosted)"
_MIN_INTERVAL = 1.0
_CACHE_SIZE = 100
_TIMEOUT = 8.0

_lock = threading.Lock()
_last_call_ts = 0.0
_cache: "OrderedDict[str, list]" = OrderedDict()


def _cache_get(key: str):
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_set(key: str, value: list) -> None:
    with _lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def geocode(query: str, limit: int = 5) -> list:
    q = (query or "").strip()
    if not q:
        return []

    key = f"{q.lower()}|{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({
        "q": q,
        "format": "json",
        "limit": str(limit),
        "addressdetails": "0",
    })
    url = f"{_NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "zh-TW,zh,en",
    })

    with _lock:
        global _last_call_ts
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.monotonic()

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Nominatim 查詢失敗: {e}")
        raise

    results = []
    for item in data if isinstance(data, list) else []:
        try:
            results.append({
                "lat": float(item["lat"]),
                "lng": float(item["lon"]),
                "display_name": item.get("display_name", ""),
            })
        except (KeyError, TypeError, ValueError):
            continue

    _cache_set(key, results)
    return results
