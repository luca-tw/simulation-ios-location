import os

from flask import Blueprint, jsonify, request

from app.services import location
from app.services.settings import SETTINGS_FILE, load_settings, merge_settings

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.post("/set-location")
def api_set_location():
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lng 格式錯誤"}), 400

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "經緯度超出範圍"}), 400

    try:
        location.set_location(lat, lng)
        merge_settings({"last_position": {"lat": lat, "lng": lng}})
        return jsonify({"ok": True, "lat": lat, "lng": lng})
    except Exception as e:
        location.logger.error(f"Web 設定位置失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/clear-location")
def api_clear_location():
    try:
        location.clear_location()
        return jsonify({"ok": True})
    except Exception as e:
        location.logger.error(f"Web 清除位置失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.get("/session-status")
def api_session_status():
    return jsonify(location.get_session_status())


@api_bp.post("/session-toggle")
def api_session_toggle():
    try:
        status = location.toggle_session_connection()
        return jsonify({"ok": True, **status})
    except Exception as e:
        location.logger.error(f"切換會話連線失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/session-connect")
def api_session_connect():
    try:
        status = location.connect_session()
        return jsonify({"ok": True, **status})
    except Exception as e:
        location.logger.error(f"建立會話連線失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/session-disconnect")
def api_session_disconnect():
    try:
        status = location.disconnect_session()
        return jsonify({"ok": True, **status})
    except Exception as e:
        location.logger.error(f"中斷會話連線失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.get("/settings")
def api_get_settings():
    try:
        settings = load_settings()
        settings["_persisted"] = os.path.exists(SETTINGS_FILE)
        return jsonify(settings)
    except Exception as e:
        location.logger.error(f"讀取設定失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/settings")
def api_save_settings():
    data = request.get_json(silent=True) or {}
    try:
        saved = merge_settings(data)
        return jsonify({"ok": True, "settings": saved})
    except Exception as e:
        location.logger.error(f"儲存設定失敗: {e}")
        return jsonify({"error": str(e)}), 500


def safe_shutdown() -> None:
    location.safe_clear_on_shutdown()
