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
