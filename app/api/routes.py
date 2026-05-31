import os

from flask import Blueprint, jsonify, request

from app.services import geocode, location, movement, routing
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
        movement.set_instant(lat, lng)
        merge_settings({"last_position": {"lat": lat, "lng": lng}})
        return jsonify({"ok": True, "lat": lat, "lng": lng})
    except Exception as e:
        location.logger.error(f"Web 設定位置失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/move")
def api_move():
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lng 格式錯誤"}), 400

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "經緯度超出範圍"}), 400

    mode = str(data.get("mode") or "instant")
    try:
        status = movement.start_move(lat, lng, mode)
        merge_settings({"last_position": {"lat": lat, "lng": lng}})
        return jsonify({"ok": True, **status})
    except Exception as e:
        location.logger.error(f"啟動移動失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/play-route")
def api_play_route():
    data = request.get_json(silent=True) or {}
    points = data.get("points")
    mode = str(data.get("mode") or "walk")
    if not isinstance(points, list):
        return jsonify({"error": "points 必須為陣列"}), 400

    try:
        status = movement.start_route(points, mode)
        return jsonify({"ok": True, **status})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        location.logger.error(f"啟動路徑播放失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/stop-movement")
def api_stop_movement():
    try:
        status = movement.stop()
        return jsonify({"ok": True, **status})
    except Exception as e:
        location.logger.error(f"停止移動失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.get("/movement-status")
def api_movement_status():
    return jsonify(movement.status())


@api_bp.post("/clear-location")
def api_clear_location():
    try:
        movement.stop()
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
    data = request.get_json(silent=True) or {}
    udid = data.get("udid") if isinstance(data, dict) else None
    try:
        status = location.connect_session(udid)
        return jsonify({"ok": True, **status})
    except Exception as e:
        location.logger.error(f"建立會話連線失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.get("/devices")
def api_list_devices():
    try:
        devices = location.list_available_devices()
        return jsonify({"devices": devices})
    except Exception as e:
        location.logger.error(f"列出裝置失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/devices/active")
def api_set_active_device():
    data = request.get_json(silent=True) or {}
    udid = str(data.get("udid") or "").strip()
    if not udid:
        return jsonify({"error": "udid 為必填"}), 400
    try:
        result = location.set_active_device(udid)
        return jsonify({"ok": True, **result})
    except Exception as e:
        location.logger.error(f"設定 active 裝置失敗: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.post("/devices/name")
def api_rename_device():
    data = request.get_json(silent=True) or {}
    udid = str(data.get("udid") or "").strip()
    name = str(data.get("name") or "").strip()
    if not udid:
        return jsonify({"error": "udid 為必填"}), 400
    try:
        result = location.rename_device(udid, name)
        return jsonify({"ok": True, **result})
    except Exception as e:
        location.logger.error(f"重新命名裝置失敗: {e}")
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


@api_bp.post("/route")
def api_get_route():
    data = request.get_json(silent=True) or {}
    try:
        from_lat = float(data.get("from_lat"))
        from_lng = float(data.get("from_lng"))
        to_lat = float(data.get("to_lat"))
        to_lng = float(data.get("to_lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "座標格式錯誤"}), 400

    mode = str(data.get("mode") or "walk")
    try:
        points = routing.get_route(from_lat, from_lng, to_lat, to_lng, mode)
        return jsonify({"ok": True, "points": points, "count": len(points)})
    except Exception as e:
        location.logger.error(f"路由查詢失敗: {e}")
        return jsonify({"error": str(e)}), 502


@api_bp.get("/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q 為必填"}), 400
    try:
        limit = max(1, min(10, int(request.args.get("limit") or 5)))
    except ValueError:
        limit = 5
    try:
        results = geocode.geocode(q, limit=limit)
        return jsonify({"results": results})
    except Exception as e:
        location.logger.error(f"地址查詢失敗: {e}")
        return jsonify({"error": str(e)}), 502


def safe_shutdown() -> None:
    location.safe_clear_on_shutdown()
