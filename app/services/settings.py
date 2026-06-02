import json
import logging
import re
from pathlib import Path
import threading

logger = logging.getLogger(__name__)

_SETTINGS_LOCK = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_FILE = _PROJECT_ROOT / "web_map_state.json"

_UDID_RE = re.compile(r"^[A-Za-z0-9-]{6,64}$")


def default_settings() -> dict:
    return {
        "map": {
            "center": {"lat": 25.033964, "lng": 121.564468},
            "zoom": 12,
        },
        "last_position": None,
        "move_mode": "instant",
        "favorites": [],
        "saved_routes": [],
        "devices": {},
        "active_udid": None,
    }


def sanitize_settings(raw: dict) -> dict:
    settings = default_settings()
    if not isinstance(raw, dict):
        return settings

    map_data = raw.get("map")
    if isinstance(map_data, dict):
        center = map_data.get("center")
        zoom = map_data.get("zoom")
        if isinstance(center, dict):
            try:
                lat = float(center.get("lat"))
                lng = float(center.get("lng"))
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    settings["map"]["center"] = {"lat": lat, "lng": lng}
            except (TypeError, ValueError):
                pass
        try:
            zoom_val = int(zoom)
            settings["map"]["zoom"] = max(1, min(20, zoom_val))
        except (TypeError, ValueError):
            pass

    last_position = raw.get("last_position")
    if isinstance(last_position, dict):
        try:
            lat = float(last_position.get("lat"))
            lng = float(last_position.get("lng"))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                settings["last_position"] = {"lat": lat, "lng": lng}
        except (TypeError, ValueError):
            pass

    move_mode = raw.get("move_mode")
    if isinstance(move_mode, str) and move_mode in {"instant", "walk", "bike", "scooter", "car", "hsr"}:
        settings["move_mode"] = move_mode

    favorites = raw.get("favorites")
    if isinstance(favorites, list):
        cleaned = []
        for item in favorites:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            try:
                lat = float(item.get("lat"))
                lng = float(item.get("lng"))
            except (TypeError, ValueError):
                continue
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                cleaned.append({"name": name[:64], "lat": lat, "lng": lng})
        settings["favorites"] = cleaned[:100]

    saved_routes = raw.get("saved_routes")
    if isinstance(saved_routes, list):
        cleaned_routes = []
        for route in saved_routes:
            if not isinstance(route, dict):
                continue
            route_name = str(route.get("name", "")).strip()
            if not route_name:
                continue
            points = route.get("points")
            if not isinstance(points, list):
                continue

            cleaned_points = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                try:
                    lat = float(point.get("lat"))
                    lng = float(point.get("lng"))
                except (TypeError, ValueError):
                    continue
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    cleaned_points.append({"lat": lat, "lng": lng})

            if len(cleaned_points) >= 2:
                cleaned_routes.append({"name": route_name[:64], "points": cleaned_points[:5000]})

        settings["saved_routes"] = cleaned_routes[:100]

    devices = raw.get("devices")
    if isinstance(devices, dict):
        cleaned_devices = {}
        for udid, info in devices.items():
            if not isinstance(udid, str) or not _UDID_RE.match(udid):
                continue
            if not isinstance(info, dict):
                continue
            name = str(info.get("name", "")).strip()[:64]
            cleaned_devices[udid] = {"name": name}
        settings["devices"] = cleaned_devices

    active_udid = raw.get("active_udid")
    if isinstance(active_udid, str) and _UDID_RE.match(active_udid):
        settings["active_udid"] = active_udid

    return settings


def load_settings() -> dict:
    with _SETTINGS_LOCK:
        if not SETTINGS_FILE.exists():
            return default_settings()
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return sanitize_settings(data)
        except Exception as e:
            logger.warning(f"讀取設定檔失敗，改用預設值: {e}")
            return default_settings()


def save_settings(settings: dict) -> dict:
    cleaned = sanitize_settings(settings)
    with _SETTINGS_LOCK:
        with SETTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    return cleaned


def merge_settings(update: dict) -> dict:
    current = load_settings()
    if not isinstance(update, dict):
        return current

    if "map" in update and isinstance(update["map"], dict):
        current["map"] = update["map"]
    if "last_position" in update:
        current["last_position"] = update["last_position"]
    if "move_mode" in update:
        current["move_mode"] = update["move_mode"]
    if "favorites" in update and isinstance(update["favorites"], list):
        current["favorites"] = update["favorites"]
    if "saved_routes" in update and isinstance(update["saved_routes"], list):
        current["saved_routes"] = update["saved_routes"]
    if "devices" in update and isinstance(update["devices"], dict):
        current["devices"] = update["devices"]
    if "active_udid" in update:
        current["active_udid"] = update["active_udid"]

    return save_settings(current)
