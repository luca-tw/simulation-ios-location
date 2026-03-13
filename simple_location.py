import logging
import asyncio
import sys
import os
import signal
import traceback
import threading
import queue
import atexit
import json
from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.cli.mounter import auto_mount
from pymobiledevice3.service_connection import ServiceConnection

try:
    from flask import Flask, jsonify, request
except ImportError:
    Flask = None
    jsonify = None
    request = None

# 自訂 RSD 類別以處理連線逾時問題
class RobustRemoteServiceDiscoveryService(RemoteServiceDiscoveryService):
    def start_lockdown_service_without_checkin(self, name: str) -> ServiceConnection:
        # 避免在 asyncio loop 中長時間阻塞：使用較短 timeout，失敗由上層決定是否重試
        timeout = 1 if name.startswith("com.apple.mobile.lockdown.remote.") else 3
        return ServiceConnection.create_using_tcp(
            self.service.address[0],
            self.get_service_port(name),
            create_connection_timeout=timeout,
        )

    async def connect(self) -> None:
        # 只建立 peer_info，避免額外嘗試 remote.lockdown 造成無意義 timeout
        await self.service.connect()
        try:
            self.peer_info = await self.service.receive_response()
            self.udid = self.peer_info["Properties"]["UniqueDeviceID"]
            self.product_type = self.peer_info["Properties"]["ProductType"]
            self.lockdown = None
            self.all_values = {}
        except Exception:
            await self.close()
            raise


def _run_tunnel_thread(udid: str, result_queue: queue.Queue, stop_event: threading.Event) -> None:
    async def _runner() -> None:
        tunnel_service = None
        lockdown_for_tunnel = None
        try:
            from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy

            lockdown_for_tunnel = create_using_usbmux(udid)
            tunnel_service = await CoreDeviceTunnelProxy.create(lockdown_for_tunnel)
            async with tunnel_service.start_tcp_tunnel() as tunnel_result:
                result_queue.put(("ready", tunnel_result.address, tunnel_result.port))
                while not stop_event.is_set():
                    await asyncio.sleep(0.2)
        except Exception as e:
            result_queue.put(("error", str(e), traceback.format_exc()))
        finally:
            if tunnel_service is not None:
                try:
                    await tunnel_service.close()
                except Exception:
                    pass
            if lockdown_for_tunnel is not None:
                try:
                    lockdown_for_tunnel.close()
                except Exception:
                    pass

    asyncio.run(_runner())

# 設定 logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_web_action_lock = threading.Lock()
_web_location_set = False
_settings_lock = threading.Lock()

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "web_map_state.json")


def _default_settings() -> dict:
    return {
        "map": {
            "center": {"lat": 25.033964, "lng": 121.564468},
            "zoom": 12,
        },
        "last_position": None,
        "favorites": [],
    }


def _sanitize_settings(raw: dict) -> dict:
    settings = _default_settings()
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

    return settings


def _load_settings() -> dict:
    with _settings_lock:
        if not os.path.exists(SETTINGS_FILE):
            return _default_settings()
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _sanitize_settings(data)
        except Exception as e:
            logger.warning(f"讀取設定檔失敗，改用預設值: {e}")
            return _default_settings()


def _save_settings(settings: dict) -> dict:
    cleaned = _sanitize_settings(settings)
    with _settings_lock:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    return cleaned


def _merge_settings(update: dict) -> dict:
    current = _load_settings()
    if not isinstance(update, dict):
        return current

    if "map" in update and isinstance(update["map"], dict):
        current["map"] = update["map"]
    if "last_position" in update:
        current["last_position"] = update["last_position"]
    if "favorites" in update and isinstance(update["favorites"], list):
        current["favorites"] = update["favorites"]

    return _save_settings(current)


class PersistentLocationSession:
    def __init__(self) -> None:
        self.lockdown = None
        self.rsd = None
        self.dvt = None
        self.sim = None
        self.tunnel_thread = None
        self.tunnel_stop_event = None
        self.connected = False

    async def ensure_connected(self) -> None:
        if self.connected:
            return

        devices = list_devices()
        if not devices:
            raise RuntimeError("未找到 iOS 裝置。請確認已連接並信任電腦。")

        udid = devices[0].serial
        logger.info(f"發現裝置: {udid}")

        self.lockdown = create_using_usbmux(udid)
        ios_version = self.lockdown.product_version
        major_version = int(ios_version.split('.')[0])

        await _prepare_developer_image(self.lockdown)

        if major_version >= 17 and sys.platform == "darwin" and os.geteuid() != 0:
            raise RuntimeError("iOS 17+ 在 macOS 需要 root 權限建立 Tunnel，請用 sudo 執行")

        if major_version < 17:
            self.dvt = DvtSecureSocketProxyService(lockdown=self.lockdown)
            self.dvt.__enter__()
            self.sim = LocationSimulation(self.dvt)
            self.connected = True
            return

        result_queue: queue.Queue = queue.Queue()
        self.tunnel_stop_event = threading.Event()
        self.tunnel_thread = threading.Thread(
            target=_run_tunnel_thread,
            args=(udid, result_queue, self.tunnel_stop_event),
            daemon=True,
        )
        self.tunnel_thread.start()

        try:
            result = result_queue.get(timeout=20)
        except queue.Empty as e:
            raise TimeoutError("等待 Tunnel 建立逾時") from e

        if result[0] == "error":
            raise RuntimeError(f"Tunnel 建立失敗: {result[1]}\\n{result[2]}")

        host, port = result[1], result[2]
        logger.info(f"Tunnel 已建立: {host}:{port}")

        self.rsd = RobustRemoteServiceDiscoveryService((host, port))
        await self.rsd.connect()

        for attempt in range(1, 4):
            try:
                self.dvt = DvtSecureSocketProxyService(self.rsd)
                self.dvt.__enter__()
                self.sim = LocationSimulation(self.dvt)
                self.connected = True
                return
            except TimeoutError as e:
                logger.warning(f"DVT 連線逾時 (第 {attempt}/3 次): {e}")
                if attempt < 3:
                    await asyncio.sleep(1)
                else:
                    raise

    async def apply(self, action: str, lat: float = None, lng: float = None) -> None:
        await self.ensure_connected()
        if action == "set":
            self.sim.set(lat, lng)
            return
        if action == "clear":
            self.sim.clear()
            return
        raise ValueError(f"不支援的動作: {action}")

    async def close(self) -> None:
        self.connected = False
        self.sim = None

        if self.dvt is not None:
            try:
                self.dvt.__exit__(None, None, None)
            except Exception:
                pass
            self.dvt = None

        if self.rsd is not None:
            try:
                await self.rsd.close()
            except Exception:
                pass
            self.rsd = None

        if self.tunnel_stop_event is not None:
            self.tunnel_stop_event.set()
            self.tunnel_stop_event = None

        if self.tunnel_thread is not None:
            self.tunnel_thread.join(timeout=2)
            self.tunnel_thread = None

        if self.lockdown is not None:
            try:
                self.lockdown.close()
            except Exception:
                pass
            self.lockdown = None

    def status(self) -> dict:
        tunnel_alive = self.tunnel_thread.is_alive() if self.tunnel_thread is not None else False
        return {
            "connected": bool(self.connected),
            "tunnel_alive": bool(tunnel_alive),
            "has_sim": self.sim is not None,
        }


_session = PersistentLocationSession()

WEB_PAGE_HTML = """<!doctype html>
<html lang=\"zh-Hant\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>iOS 即時定位地圖</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" integrity=\"sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=\" crossorigin=\"\" />
    <style>
        :root { --bg:#f5f7fb; --card:#ffffff; --ink:#102136; --muted:#5b6b7c; --brand:#0f766e; --warn:#b45309; }
        html, body { height: 100%; margin: 0; background: radial-gradient(circle at 20% 20%, #eef6ff, #f7fafc 60%, #eef2ff 100%); color: var(--ink); font-family: \"Avenir Next\", \"PingFang TC\", \"Noto Sans TC\", sans-serif; }
        .wrap { display:flex; flex-direction:column; height:100%; }
        .top { padding:12px 14px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; background:rgba(255,255,255,.78); backdrop-filter: blur(6px); border-bottom:1px solid #d7e3ee; }
        .title { font-size:16px; font-weight:700; letter-spacing:.4px; }
        .status { font-size:13px; color:var(--muted); flex:1; }
        .session-pill { display:flex; align-items:center; gap:6px; padding:6px 10px; border:1px solid #d5dee8; border-radius:999px; background:#fff; font-size:12px; color:#334155; }
        .session-dot { width:8px; height:8px; border-radius:50%; background:#94a3b8; }
        .session-dot.ok { background:#16a34a; }
        .session-dot.off { background:#ef4444; }
        .btn { border:0; background:var(--brand); color:#fff; border-radius:8px; padding:8px 12px; cursor:pointer; font-weight:600; }
        .btn.secondary { background:#334155; }
        .coord { display:flex; gap:8px; align-items:center; }
        .coord input { width:140px; border:1px solid #c7d7e8; border-radius:8px; padding:8px; font-size:13px; }
        .coord select { border:1px solid #c7d7e8; border-radius:8px; padding:8px; font-size:13px; background:#fff; }
        .coord .fav-select { width:180px; }
        .tools { display:flex; gap:8px; align-items:center; }
        .btn.tool { background:#1f2937; }
        .btn.tool.active { background:#b45309; }
        #map { flex:1; }
        .hint { position:absolute; right:12px; bottom:12px; z-index:500; background:rgba(16,33,54,.88); color:#fff; padding:8px 10px; border-radius:8px; font-size:12px; }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"top\">
            <div class=\"title\">iOS 即時定位</div>
            <div class=\"coord\">
                <input id=\"latInput\" type=\"number\" step=\"any\" placeholder=\"緯度 Lat\" />
                <input id=\"lngInput\" type=\"number\" step=\"any\" placeholder=\"經度 Lng\" />
                <select id=\"moveMode\">
                    <option value=\"instant\">即時傳送</option>
                    <option value=\"smooth\">平滑移動</option>
                </select>
                <button id=\"applyBtn\" class=\"btn\">套用座標</button>
                <select id="favoriteSelect" class="fav-select">
                    <option value="">收藏地點</option>
                </select>
                <button id="saveFavoriteBtn" class="btn secondary">收藏目前點</button>
                <button id="goFavoriteBtn" class="btn secondary">前往收藏</button>
                <button id="deleteFavoriteBtn" class="btn secondary">刪除收藏</button>
            </div>
            <div class=\"tools\">
                <button id=\"drawRouteBtn\" class=\"btn tool\">畫路徑</button>
                <button id=\"clearRouteBtn\" class=\"btn tool\">清空路徑</button>
                <button id=\"playRouteBtn\" class=\"btn tool\">播放路徑</button>
                <button id=\"stopRouteBtn\" class=\"btn tool\">停止播放</button>
                <button id=\"importGpxBtn\" class=\"btn tool\">匯入 GPX</button>
                <button id=\"exportGpxBtn\" class=\"btn tool\">匯出 GPX</button>
                <input id=\"gpxFileInput\" type=\"file\" accept=\".gpx,application/gpx+xml,text/xml,application/xml\" style=\"display:none\" />
            </div>
            <div id="sessionPill" class="session-pill"><span id="sessionDot" class="session-dot"></span><span id="sessionText">會話狀態：檢查中</span></div>
            <div id=\"status\" class=\"status\">點地圖或輸入經緯度即可更新手機定位</div>
            <button id=\"clearBtn\" class=\"btn secondary\">恢復真實位置</button>
        </div>
        <div id=\"map\"></div>
    </div>
    <div class=\"hint\">資料來源: OpenStreetMap</div>

    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\" integrity=\"sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=\" crossorigin=\"\"></script>
    <script>
        const statusEl = document.getElementById('status');
        const sessionDot = document.getElementById('sessionDot');
        const sessionText = document.getElementById('sessionText');
        const latInput = document.getElementById('latInput');
        const lngInput = document.getElementById('lngInput');
        const moveModeEl = document.getElementById('moveMode');
        const drawRouteBtn = document.getElementById('drawRouteBtn');
        const clearRouteBtn = document.getElementById('clearRouteBtn');
        const playRouteBtn = document.getElementById('playRouteBtn');
        const stopRouteBtn = document.getElementById('stopRouteBtn');
        const favoriteSelect = document.getElementById('favoriteSelect');
        const saveFavoriteBtn = document.getElementById('saveFavoriteBtn');
        const goFavoriteBtn = document.getElementById('goFavoriteBtn');
        const deleteFavoriteBtn = document.getElementById('deleteFavoriteBtn');
        const importGpxBtn = document.getElementById('importGpxBtn');
        const exportGpxBtn = document.getElementById('exportGpxBtn');
        const gpxFileInput = document.getElementById('gpxFileInput');
        const map = L.map('map', { zoomControl: true }).setView([25.033964, 121.564468], 12);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
        }).addTo(map);

        let marker = null;
        let busy = false;
        let pendingLocation = null;
        const SPEED_KMH = 18;
        const SPEED_MPS = SPEED_KMH / 3.6;
        const KEY_TICK_MS = 200;
        const STEP_METERS = SPEED_MPS * (KEY_TICK_MS / 1000);
        const pressed = new Set();
        let moveTimer = null;
        let smoothTarget = null;
        let targetMarker = null;
        let targetLine = null;
        let drawRouteMode = false;
        let routePoints = [];
        let routeLine = null;
        let routePlaybackActive = false;
        let routePlaybackIndex = 0;
        let appSettings = { map: { center: { lat: 25.033964, lng: 121.564468 }, zoom: 12 }, last_position: null, favorites: [] };
        let settingsSaveTimer = null;

        function setStatus(text, warn = false) {
            statusEl.textContent = text;
            statusEl.style.color = warn ? '#b45309' : '#5b6b7c';
        }

        function setSessionState(connected, tunnelAlive) {
            sessionDot.classList.remove('ok', 'off');
            if (connected) {
                sessionDot.classList.add('ok');
                sessionText.textContent = tunnelAlive ? '會話已連線（Tunnel 活躍）' : '會話已連線';
            } else {
                sessionDot.classList.add('off');
                sessionText.textContent = '會話已斷線';
            }
        }

        async function refreshSessionStatus() {
            try {
                const res = await fetch('/api/session-status');
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'status error');
                setSessionState(Boolean(data.connected), Boolean(data.tunnel_alive));
            } catch (err) {
                setSessionState(false, false);
            }
        }

        function getMapState() {
            const center = map.getCenter();
            return {
                center: clampLatLng(center.lat, center.lng),
                zoom: Math.max(1, Math.min(20, map.getZoom())),
            };
        }

        function renderFavorites() {
            const prev = favoriteSelect.value;
            favoriteSelect.innerHTML = '<option value="">收藏地點</option>';
            appSettings.favorites.forEach((fav, idx) => {
                const option = document.createElement('option');
                option.value = String(idx);
                option.textContent = `${fav.name} (${fav.lat.toFixed(4)}, ${fav.lng.toFixed(4)})`;
                favoriteSelect.appendChild(option);
            });
            if (prev && Number(prev) < appSettings.favorites.length) {
                favoriteSelect.value = prev;
            }
        }

        async function saveSettingsPartial(partial) {
            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(partial),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'settings save error');
                if (data.settings) {
                    appSettings = data.settings;
                    renderFavorites();
                }
            } catch (err) {
                console.warn('save settings failed', err);
            }
        }

        function flushSettingsOnLeave() {
            const payload = {
                map: getMapState(),
                last_position: currentLatLng(),
                favorites: appSettings.favorites,
            };

            try {
                const body = JSON.stringify(payload);
                if (navigator.sendBeacon) {
                    const blob = new Blob([body], { type: 'application/json' });
                    navigator.sendBeacon('/api/settings', blob);
                    return;
                }
                fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body,
                    keepalive: true,
                });
            } catch (err) {
                console.warn('flush settings failed', err);
            }
        }

        function scheduleSaveMapState() {
            if (settingsSaveTimer) {
                clearTimeout(settingsSaveTimer);
            }
            settingsSaveTimer = setTimeout(() => {
                settingsSaveTimer = null;
                saveSettingsPartial({ map: getMapState() });
            }, 350);
        }

        async function loadSettings() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'settings load error');
                appSettings = data;
                renderFavorites();

                if (appSettings.map && appSettings.map.center) {
                    const center = appSettings.map.center;
                    const zoom = Number(appSettings.map.zoom || 12);
                    map.setView([center.lat, center.lng], Math.max(1, Math.min(20, zoom)), { animate: false });
                    setInputs(center.lat, center.lng);
                }

                if (appSettings.last_position) {
                    const p = appSettings.last_position;
                    if (!marker) marker = L.marker([p.lat, p.lng]).addTo(map);
                    marker.setLatLng([p.lat, p.lng]);
                }
                return Boolean(data._persisted);
            } catch (err) {
                setStatus(`讀取設定失敗，使用預設值`, true);
                return false;
            }
        }

        function upsertFavorite(name, lat, lng) {
            const normalized = String(name || '').trim();
            if (!normalized) {
                setStatus('收藏名稱不可為空', true);
                return;
            }
            const clamped = clampLatLng(lat, lng);
            const idx = appSettings.favorites.findIndex((f) => f.name === normalized);
            if (idx >= 0) {
                appSettings.favorites[idx] = { name: normalized, lat: clamped.lat, lng: clamped.lng };
            } else {
                appSettings.favorites.push({ name: normalized, lat: clamped.lat, lng: clamped.lng });
            }
            renderFavorites();
            saveSettingsPartial({ favorites: appSettings.favorites });
            setStatus(`已收藏地點: ${normalized}`);
        }

        function deleteSelectedFavorite() {
            const idx = Number(favoriteSelect.value);
            if (Number.isNaN(idx) || idx < 0 || idx >= appSettings.favorites.length) {
                setStatus('請先選擇要刪除的收藏地點', true);
                return;
            }
            const name = appSettings.favorites[idx].name;
            appSettings.favorites.splice(idx, 1);
            renderFavorites();
            saveSettingsPartial({ favorites: appSettings.favorites });
            setStatus(`已刪除收藏地點: ${name}`);
        }

        function metersToLatDelta(meters) {
            return meters / 111320;
        }

        function metersToLngDelta(meters, latitude) {
            const cosLat = Math.cos((latitude * Math.PI) / 180);
            const denom = Math.max(1e-6, 111320 * Math.abs(cosLat));
            return meters / denom;
        }

        function clampLatLng(lat, lng) {
            const clampedLat = Math.max(-90, Math.min(90, lat));
            let wrappedLng = lng;
            if (wrappedLng > 180) wrappedLng -= 360;
            if (wrappedLng < -180) wrappedLng += 360;
            return { lat: clampedLat, lng: wrappedLng };
        }

        function setInputs(lat, lng) {
            latInput.value = Number(lat).toFixed(6);
            lngInput.value = Number(lng).toFixed(6);
        }

        function setInitialView(lat, lng) {
            const clamped = clampLatLng(lat, lng);
            setInputs(clamped.lat, clamped.lng);
            map.setView([clamped.lat, clamped.lng], 16, { animate: false });
            if (!marker) marker = L.marker([clamped.lat, clamped.lng]).addTo(map);
            marker.setLatLng([clamped.lat, clamped.lng]);
        }

        function initFromBrowserGps() {
            if (!navigator.geolocation) {
                setStatus('瀏覽器不支援 GPS，使用預設初始位置', true);
                return;
            }

            setStatus('正在請求網頁 GPS 位置...');
            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    const lat = pos.coords.latitude;
                    const lng = pos.coords.longitude;
                    setInitialView(lat, lng);
                    scheduleSaveMapState();
                    setStatus(`已使用網頁 GPS 作為初始位置: ${lat.toFixed(6)}, ${lng.toFixed(6)}`);
                },
                (err) => {
                    const msg = err && err.message ? err.message : '未知錯誤';
                    setStatus(`無法取得網頁 GPS，使用預設初始位置: ${msg}`, true);
                },
                {
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 30000,
                }
            );
        }

        function updateDrawRouteButton() {
            drawRouteBtn.classList.toggle('active', drawRouteMode);
            drawRouteBtn.textContent = drawRouteMode ? '結束繪製' : '畫路徑';
        }

        function updatePlaybackButtons() {
            playRouteBtn.classList.toggle('active', routePlaybackActive);
            playRouteBtn.textContent = routePlaybackActive ? '播放中...' : '播放路徑';
        }

        function updateRouteVisuals() {
            if (routePoints.length < 2) {
                if (routeLine) {
                    map.removeLayer(routeLine);
                    routeLine = null;
                }
                return;
            }

            const latlngs = routePoints.map((p) => [p.lat, p.lng]);
            if (!routeLine) {
                routeLine = L.polyline(latlngs, {
                    color: '#2563eb',
                    weight: 4,
                    opacity: 0.85,
                }).addTo(map);
            } else {
                routeLine.setLatLngs(latlngs);
            }
        }

        function clearRoute() {
            routePoints = [];
            routePlaybackActive = false;
            routePlaybackIndex = 0;
            smoothTarget = null;
            pendingLocation = null;
            if (routeLine) {
                map.removeLayer(routeLine);
                routeLine = null;
            }
            updatePlaybackButtons();
        }

        function addRoutePoint(lat, lng) {
            routePoints.push(clampLatLng(lat, lng));
            updateRouteVisuals();
            setStatus(`路徑點已加入（${routePoints.length} 點）`);
        }

        function stopRoutePlayback(showStatus = true) {
            routePlaybackActive = false;
            routePlaybackIndex = 0;
            smoothTarget = null;
            pendingLocation = null;
            updatePlaybackButtons();
            if (showStatus) {
                setStatus(`已停止路徑播放（速度上限 ${SPEED_KMH} km/h）`);
            }
        }

        function startRoutePlayback() {
            if (routePoints.length < 2) {
                setStatus('請先畫至少 2 個路徑點才能播放', true);
                return;
            }

            drawRouteMode = false;
            updateDrawRouteButton();
            routePlaybackActive = true;
            routePlaybackIndex = 0;
            smoothTarget = routePoints[routePlaybackIndex];
            updateSmoothVisuals(currentLatLng(), smoothTarget);
            updatePlaybackButtons();
            startMoveTimer();
            setStatus(`開始沿路徑播放（速度上限 ${SPEED_KMH} km/h）`);
        }

        function buildGpx(points) {
            const now = new Date().toISOString();
            const trkpts = points
                .map((p) => `        <trkpt lat="${p.lat.toFixed(8)}" lon="${p.lng.toFixed(8)}"><time>${now}</time></trkpt>`)
                .join('\\n');

            return `<?xml version="1.0" encoding="UTF-8"?>\n` +
                `<gpx version="1.1" creator="simple-ios-location" xmlns="http://www.topografix.com/GPX/1/1">\n` +
                `  <trk>\n` +
                `    <name>simple-ios-location route</name>\n` +
                `    <trkseg>\n${trkpts}\n    </trkseg>\n` +
                `  </trk>\n` +
                `</gpx>\n`;
        }

        function downloadTextFile(filename, text) {
            const blob = new Blob([text], { type: 'application/gpx+xml;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            a.click();
            URL.revokeObjectURL(url);
        }

        function parseGpxText(gpxText) {
            const parser = new DOMParser();
            const xmlDoc = parser.parseFromString(gpxText, 'application/xml');
            if (xmlDoc.querySelector('parsererror')) {
                throw new Error('GPX 格式無法解析');
            }

            const result = [];
            const trkpts = xmlDoc.getElementsByTagName('trkpt');
            const rtepts = xmlDoc.getElementsByTagName('rtept');
            const points = trkpts.length > 0 ? trkpts : rtepts;

            for (let i = 0; i < points.length; i += 1) {
                const node = points[i];
                const lat = Number(node.getAttribute('lat'));
                const lng = Number(node.getAttribute('lon'));
                if (Number.isNaN(lat) || Number.isNaN(lng)) {
                    continue;
                }
                result.push(clampLatLng(lat, lng));
            }

            if (result.length < 2) {
                throw new Error('GPX 至少需要 2 個路徑點');
            }

            return result;
        }

        function clearSmoothVisuals() {
            if (targetMarker) {
                map.removeLayer(targetMarker);
                targetMarker = null;
            }
            if (targetLine) {
                map.removeLayer(targetLine);
                targetLine = null;
            }
        }

        function updateSmoothVisuals(current, target) {
            if (!targetMarker) {
                targetMarker = L.marker([target.lat, target.lng]).addTo(map);
                targetMarker.bindTooltip('目的地旗標', { permanent: true, direction: 'top', offset: [0, -10] });
            } else {
                targetMarker.setLatLng([target.lat, target.lng]);
            }

            if (!targetLine) {
                targetLine = L.polyline([[current.lat, current.lng], [target.lat, target.lng]], {
                    color: '#0f766e',
                    weight: 4,
                    opacity: 0.8,
                    dashArray: '10, 8'
                }).addTo(map);
            } else {
                targetLine.setLatLngs([[current.lat, current.lng], [target.lat, target.lng]]);
            }
        }

        function applyLocalPosition(lat, lng) {
            if (!marker) marker = L.marker([lat, lng]).addTo(map);
            marker.setLatLng([lat, lng]);
            setInputs(lat, lng);
            map.panTo([lat, lng], { animate: false });
        }

        async function sendLocation(lat, lng, options = {}) {
            const { silent = false } = options;
            if (busy) {
                pendingLocation = { lat, lng, silent };
                return;
            }
            busy = true;
            if (!silent) {
                setStatus('正在更新定位中...');
            }
            try {
                const res = await fetch('/api/set-location', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ lat, lng })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || '設定失敗');

                applyLocalPosition(lat, lng);
                appSettings.last_position = { lat, lng };
                if (!silent) {
                    scheduleSaveMapState();
                    saveSettingsPartial({ last_position: { lat, lng } });
                    setStatus(`定位已更新: ${lat.toFixed(6)}, ${lng.toFixed(6)} (WASD 最高 ${SPEED_KMH} km/h)`);
                    await refreshSessionStatus();
                }
            } catch (err) {
                setStatus(`更新失敗: ${err.message}`, true);
                await refreshSessionStatus();
            } finally {
                busy = false;
                if (pendingLocation) {
                    const next = pendingLocation;
                    pendingLocation = null;
                    sendLocation(next.lat, next.lng, { silent: next.silent });
                }
            }
        }

        function startSmoothMoveTo(lat, lng) {
            smoothTarget = clampLatLng(lat, lng);
            updateSmoothVisuals(currentLatLng(), smoothTarget);
            startMoveTimer();
        }

        async function moveByMode(lat, lng) {
            routePlaybackActive = false;
            updatePlaybackButtons();
            if (moveModeEl.value === 'smooth') {
                startSmoothMoveTo(lat, lng);
                return;
            }
            smoothTarget = null;
            clearSmoothVisuals();
            await sendLocation(lat, lng);
        }

        map.on('click', (e) => {
            const { lat, lng } = e.latlng;
            if (drawRouteMode) {
                addRoutePoint(lat, lng);
                return;
            }
            moveByMode(lat, lng);
        });

        document.getElementById('applyBtn').addEventListener('click', async () => {
            const lat = Number(latInput.value);
            const lng = Number(lngInput.value);
            if (Number.isNaN(lat) || Number.isNaN(lng)) {
                setStatus('請先輸入有效經緯度', true);
                return;
            }
            if (lat < -90 || lat > 90 || lng < -180 || lng > 180) {
                setStatus('經緯度超出範圍', true);
                return;
            }
            map.panTo([lat, lng]);
            await moveByMode(lat, lng);
        });

        saveFavoriteBtn.addEventListener('click', () => {
            const p = currentLatLng();
            const suggested = `收藏-${new Date().toLocaleString('zh-TW', { hour12: false })}`;
            const name = window.prompt('請輸入收藏名稱', suggested);
            if (name === null) return;
            upsertFavorite(name, p.lat, p.lng);
        });

        goFavoriteBtn.addEventListener('click', async () => {
            const idx = Number(favoriteSelect.value);
            if (Number.isNaN(idx) || idx < 0 || idx >= appSettings.favorites.length) {
                setStatus('請先選擇收藏地點', true);
                return;
            }
            const p = appSettings.favorites[idx];
            map.panTo([p.lat, p.lng]);
            await moveByMode(p.lat, p.lng);
        });

        deleteFavoriteBtn.addEventListener('click', () => {
            deleteSelectedFavorite();
        });

        drawRouteBtn.addEventListener('click', () => {
            drawRouteMode = !drawRouteMode;
            updateDrawRouteButton();
            setStatus(drawRouteMode ? '路徑繪製模式：點地圖可新增路徑點' : '已離開路徑繪製模式');
        });

        clearRouteBtn.addEventListener('click', () => {
            clearRoute();
            setStatus('路徑已清空');
        });

        playRouteBtn.addEventListener('click', () => {
            startRoutePlayback();
        });

        stopRouteBtn.addEventListener('click', () => {
            stopRoutePlayback();
            clearSmoothVisuals();
        });

        exportGpxBtn.addEventListener('click', () => {
            if (routePoints.length < 2) {
                setStatus('請先畫至少 2 個路徑點才能匯出 GPX', true);
                return;
            }
            const gpx = buildGpx(routePoints);
            const stamp = new Date().toISOString().replace(/[:.]/g, '-');
            downloadTextFile(`route-${stamp}.gpx`, gpx);
            setStatus(`GPX 已匯出（${routePoints.length} 點）`);
        });

        importGpxBtn.addEventListener('click', () => {
            gpxFileInput.click();
        });

        gpxFileInput.addEventListener('change', async () => {
            const file = gpxFileInput.files && gpxFileInput.files[0];
            if (!file) return;

            try {
                const text = await file.text();
                routePoints = parseGpxText(text);
                updateRouteVisuals();
                const first = routePoints[0];
                const last = routePoints[routePoints.length - 1];
                map.fitBounds([[first.lat, first.lng], [last.lat, last.lng]], { padding: [30, 30] });
                setStatus(`GPX 匯入成功（${routePoints.length} 點）`);
            } catch (err) {
                setStatus(`GPX 匯入失敗: ${err.message}`, true);
            } finally {
                gpxFileInput.value = '';
            }
        });

        function currentLatLng() {
            if (marker) {
                const p = marker.getLatLng();
                return { lat: p.lat, lng: p.lng };
            }
            const lat = Number(latInput.value);
            const lng = Number(lngInput.value);
            if (!Number.isNaN(lat) && !Number.isNaN(lng)) {
                return { lat, lng };
            }
            return { lat: 25.033964, lng: 121.564468 };
        }

        async function tickMove() {
            const now = currentLatLng();
            let nextLat = now.lat;
            let nextLng = now.lng;
            let moved = false;
            const latStep = metersToLatDelta(STEP_METERS);
            const lngStep = metersToLngDelta(STEP_METERS, now.lat);

            const y = (pressed.has('w') ? 1 : 0) - (pressed.has('s') ? 1 : 0);
            const x = (pressed.has('d') ? 1 : 0) - (pressed.has('a') ? 1 : 0);
            const mag = Math.hypot(x, y);
            if (mag > 0) {
                const nx = x / mag;
                const ny = y / mag;
                nextLat += ny * latStep;
                nextLng += nx * lngStep;
                moved = true;
                smoothTarget = null;
            } else if (smoothTarget) {
                const dyMeters = (smoothTarget.lat - now.lat) * 111320;
                const dxMeters = (smoothTarget.lng - now.lng) * 111320 * Math.cos((now.lat * Math.PI) / 180);
                const dist = Math.hypot(dxMeters, dyMeters);

                if (dist <= STEP_METERS) {
                    nextLat = smoothTarget.lat;
                    nextLng = smoothTarget.lng;
                    if (routePlaybackActive) {
                        routePlaybackIndex += 1;
                        if (routePlaybackIndex < routePoints.length) {
                            smoothTarget = routePoints[routePlaybackIndex];
                        } else {
                            routePlaybackActive = false;
                            routePlaybackIndex = 0;
                            smoothTarget = null;
                            updatePlaybackButtons();
                            setStatus(`路徑播放完成（速度上限 ${SPEED_KMH} km/h）`);
                        }
                    } else {
                        smoothTarget = null;
                    }
                    moved = true;
                } else {
                    const nx = dxMeters / dist;
                    const ny = dyMeters / dist;
                    nextLat += metersToLatDelta(ny * STEP_METERS);
                    nextLng += metersToLngDelta(nx * STEP_METERS, now.lat);
                    moved = true;
                }
            }

            if (!moved) {
                stopMoveTimerIfIdle();
                return;
            }

            const clamped = clampLatLng(nextLat, nextLng);
            if (smoothTarget) {
                updateSmoothVisuals(clamped, smoothTarget);
            }
            applyLocalPosition(clamped.lat, clamped.lng);
            await sendLocation(clamped.lat, clamped.lng, { silent: true });

            if (pressed.size === 0 && !smoothTarget) {
                if (targetLine) {
                    map.removeLayer(targetLine);
                    targetLine = null;
                }
                stopMoveTimerIfIdle();
            }
        }

        function shouldIgnoreKeyEvent(evt) {
            const tag = (evt.target && evt.target.tagName) ? evt.target.tagName.toLowerCase() : '';
            return tag === 'input' || tag === 'textarea';
        }

        function startMoveTimer() {
            if (moveTimer) return;
            moveTimer = setInterval(() => {
                tickMove();
            }, KEY_TICK_MS);
        }

        function stopMoveTimerIfIdle() {
            if (pressed.size === 0 && !smoothTarget && moveTimer) {
                clearInterval(moveTimer);
                moveTimer = null;
            }
        }

        window.addEventListener('keydown', (evt) => {
            if (shouldIgnoreKeyEvent(evt)) return;
            const key = evt.key.toLowerCase();
            if (!['w', 'a', 's', 'd'].includes(key)) return;
            evt.preventDefault();
            routePlaybackActive = false;
            updatePlaybackButtons();
            pressed.add(key);
            startMoveTimer();
        });

        window.addEventListener('keyup', (evt) => {
            const key = evt.key.toLowerCase();
            if (!['w', 'a', 's', 'd'].includes(key)) return;
            pressed.delete(key);
            stopMoveTimerIfIdle();
        });

        window.addEventListener('blur', () => {
            pressed.clear();
            stopMoveTimerIfIdle();
        });

        moveModeEl.addEventListener('change', () => {
            if (moveModeEl.value === 'instant') {
                routePlaybackActive = false;
                updatePlaybackButtons();
                smoothTarget = null;
                clearSmoothVisuals();
            }
            setStatus(`模式: ${moveModeEl.value === 'smooth' ? '平滑移動' : '即時傳送'}，速度上限 ${SPEED_KMH} km/h`);
        });

        map.on('moveend zoomend', scheduleSaveMapState);

        window.addEventListener('beforeunload', flushSettingsOnLeave);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                flushSettingsOnLeave();
            }
        });

        async function bootstrap() {
            setInitialView(25.033964, 121.564468);
            updateDrawRouteButton();
            updatePlaybackButtons();
            setStatus(`模式: 即時傳送，速度上限 ${SPEED_KMH} km/h`);
            const hasPersistedSettings = await loadSettings();
            if (!hasPersistedSettings) {
                initFromBrowserGps();
            }
        }

        bootstrap();

        document.getElementById('clearBtn').addEventListener('click', async () => {
            if (busy) return;
            busy = true;
            routePlaybackActive = false;
            updatePlaybackButtons();
            smoothTarget = null;
            clearSmoothVisuals();
            setStatus('正在恢復真實位置...');
            try {
                const res = await fetch('/api/clear-location', { method: 'POST' });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || '清除失敗');
                if (marker) { map.removeLayer(marker); marker = null; }
                setStatus(`已恢復真實位置 (WASD 最高 ${SPEED_KMH} km/h)`);
                await refreshSessionStatus();
            } catch (err) {
                setStatus(`恢復失敗: ${err.message}`, true);
                await refreshSessionStatus();
            } finally {
                busy = false;
            }
        });

        refreshSessionStatus();
        setInterval(refreshSessionStatus, 2000);
    </script>
</body>
</html>
"""


def _apply_location_action_sync(simulation, action: str, lat: float = None, lng: float = None) -> None:
    if action == "set":
        simulation.set(lat, lng)
        return
    if action == "clear":
        simulation.clear()
        return
    raise ValueError(f"不支援的動作: {action}")


async def _prepare_developer_image(lockdown) -> None:
    try:
        await auto_mount(lockdown)
        logger.info("Image 掛載成功 (或已掛載)")
    except Exception as e:
        logger.warning(f"掛載 Image 時發生警告 (可能已掛載): {e}")


async def _apply_location_action(action: str, lat: float = None, lng: float = None) -> None:
    try:
        await _session.apply(action, lat, lng)
    except Exception:
        # 連線失效時重建一次會話再重試
        await _session.close()
        await _session.apply(action, lat, lng)


def _safe_clear_from_web_shutdown() -> None:
    global _web_location_set
    if not _web_location_set:
        try:
            with _web_action_lock:
                asyncio.run(_session.close())
        except Exception:
            pass
        return
    try:
        with _web_action_lock:
            asyncio.run(_apply_location_action("clear"))
            asyncio.run(_session.close())
        logger.info("Web 模式結束前已恢復真實位置。")
    except Exception as e:
        logger.error(f"Web 模式關閉時恢復位置失敗: {e}")
    finally:
        _web_location_set = False


def create_web_app():
    if Flask is None:
        raise RuntimeError("缺少 Flask，請先安裝 requirements.txt")

    app = Flask(__name__)

    @app.get("/")
    def index():
        return WEB_PAGE_HTML

    @app.post("/api/set-location")
    def api_set_location():
        global _web_location_set
        data = request.get_json(silent=True) or {}
        try:
            lat = float(data.get("lat"))
            lng = float(data.get("lng"))
        except (TypeError, ValueError):
            return jsonify({"error": "lat/lng 格式錯誤"}), 400

        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return jsonify({"error": "經緯度超出範圍"}), 400

        try:
            with _web_action_lock:
                asyncio.run(_apply_location_action("set", lat, lng))
            _web_location_set = True
            _merge_settings({"last_position": {"lat": lat, "lng": lng}})
            return jsonify({"ok": True, "lat": lat, "lng": lng})
        except Exception as e:
            logger.error(f"Web 設定位置失敗: {e}")
            return jsonify({"error": str(e)}), 500

    @app.post("/api/clear-location")
    def api_clear_location():
        global _web_location_set
        try:
            with _web_action_lock:
                asyncio.run(_apply_location_action("clear"))
            _web_location_set = False
            return jsonify({"ok": True})
        except Exception as e:
            logger.error(f"Web 清除位置失敗: {e}")
            return jsonify({"error": str(e)}), 500

    @app.get("/api/session-status")
    def api_session_status():
        status = _session.status()
        return jsonify(status)

    @app.get("/api/settings")
    def api_get_settings():
        try:
            settings = _load_settings()
            settings["_persisted"] = os.path.exists(SETTINGS_FILE)
            return jsonify(settings)
        except Exception as e:
            logger.error(f"讀取設定失敗: {e}")
            return jsonify({"error": str(e)}), 500

    @app.post("/api/settings")
    def api_save_settings():
        data = request.get_json(silent=True) or {}
        try:
            saved = _merge_settings(data)
            return jsonify({"ok": True, "settings": saved})
        except Exception as e:
            logger.error(f"儲存設定失敗: {e}")
            return jsonify({"error": str(e)}), 500

    return app

def _handle_termination_signal(signum, frame):
    try:
        _safe_clear_from_web_shutdown()
    except Exception as e:
        logger.error(f"結束前清理失敗: {e}")
    logger.info(f"收到結束訊號 ({signum})，正在停止模擬...")
    raise KeyboardInterrupt

if __name__ == "__main__":
    host = os.environ.get("SIMPLE_LOCATION_HOST", "127.0.0.1")
    port = int(os.environ.get("SIMPLE_LOCATION_PORT", "8000"))

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    signal.signal(signal.SIGINT, _handle_termination_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_termination_signal)

    atexit.register(_safe_clear_from_web_shutdown)
    app = create_web_app()
    logger.info(f"Web 模式啟動: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
