import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

OSRM_BASE = "https://router.project-osrm.org"

_PROFILE = {
    "walk": "walking",
    "bike": "cycling",
    "scooter": "driving",
    "car": "driving",
    "hsr": "driving",
}


def get_route(from_lat: float, from_lng: float, to_lat: float, to_lng: float, mode: str = "walk") -> list:
    profile = _PROFILE.get(mode, "driving")
    url = (
        f"{OSRM_BASE}/route/v1/{profile}"
        f"/{from_lng},{from_lat};{to_lng},{to_lat}"
        f"?overview=full&geometries=geojson"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "simulation-ios-location/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連線到路由服務: {e}") from e

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"路由服務回應錯誤: {data.get('code', 'unknown')}")

    coords = data["routes"][0]["geometry"]["coordinates"]  # [[lng, lat], ...]
    return [{"lat": c[1], "lng": c[0]} for c in coords]


_MAX_WAYPOINTS_PER_REQUEST = 90


def _fetch_osrm(coords: list, profile: str) -> list:
    path = ";".join(f"{c['lng']},{c['lat']}" for c in coords)
    url = f"{OSRM_BASE}/route/v1/{profile}/{path}?overview=full&geometries=geojson"
    req = urllib.request.Request(url, headers={"User-Agent": "simulation-ios-location/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"無法連線到路由服務: {e}") from e

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"路由服務回應錯誤: {data.get('code', 'unknown')}")

    return [{"lat": c[1], "lng": c[0]} for c in data["routes"][0]["geometry"]["coordinates"]]


def get_route_through(waypoints: list, mode: str = "walk") -> list:
    """Road route passing through every waypoint in order (chunked for OSRM limits)."""
    if len(waypoints) < 2:
        raise ValueError("至少需要 2 個座標")
    profile = _PROFILE.get(mode, "driving")
    result: list = []
    step = _MAX_WAYPOINTS_PER_REQUEST - 1
    for start in range(0, len(waypoints) - 1, step):
        chunk = waypoints[start:start + _MAX_WAYPOINTS_PER_REQUEST]
        if len(chunk) < 2:
            break
        segment = _fetch_osrm(chunk, profile)
        result.extend(segment[1:] if result else segment)
    return result
