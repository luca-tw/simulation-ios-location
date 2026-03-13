import asyncio
import logging
import os
import queue
import sys
import threading
import traceback

from pymobiledevice3.cli.mounter import auto_mount
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.service_connection import ServiceConnection
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.usbmux import list_devices

logger = logging.getLogger(__name__)

_WEB_ACTION_LOCK = threading.Lock()
_WEB_LOCATION_SET = False


# Start a background event loop
_loop = asyncio.new_event_loop()
def _loop_thread_run():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

_bg_thread = threading.Thread(target=_loop_thread_run, daemon=True)
_bg_thread.start()


def _run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()


class RobustRemoteServiceDiscoveryService(RemoteServiceDiscoveryService):
    def start_lockdown_service_without_checkin(self, name: str) -> ServiceConnection:
        timeout = 1 if name.startswith("com.apple.mobile.lockdown.remote.") else 3
        return ServiceConnection.create_using_tcp(
            self.service.address[0],
            self.get_service_port(name),
            create_connection_timeout=timeout,
        )

    async def connect(self) -> None:
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

            lockdown_for_tunnel = await create_using_usbmux(udid)
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
                    await lockdown_for_tunnel.close()
                except Exception:
                    pass

    asyncio.run(_runner())


async def _prepare_developer_image(lockdown) -> None:
    try:
        await auto_mount(lockdown)
        logger.info("Image 掛載成功 (或已掛載)")
    except Exception as e:
        logger.warning(f"掛載 Image 時發生警告 (可能已掛載): {e}")


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

        devices = await list_devices()
        if not devices:
            raise RuntimeError("未找到 iOS 裝置。請確認已連接並信任電腦。")

        udid = devices[0].serial
        logger.info(f"發現裝置: {udid}")

        self.lockdown = await create_using_usbmux(udid)
        ios_version = self.lockdown.product_version
        major_version = int(ios_version.split('.')[0])

        await _prepare_developer_image(self.lockdown)

        if major_version >= 17 and sys.platform == "darwin" and os.geteuid() != 0:
            raise RuntimeError("iOS 17+ 在 macOS 需要 root 權限建立 Tunnel，請用 sudo 執行")

        if major_version < 17:
            self.dvt = DvtProvider(lockdown=self.lockdown)
            await self.dvt.connect()
            self.sim = LocationSimulation(self.dvt)
            await self.sim.connect()
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
                self.dvt = DvtProvider(self.rsd)
                await self.dvt.connect()
                self.sim = LocationSimulation(self.dvt)
                await self.sim.connect()
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
            await self.sim.set(lat, lng)
            return
        if action == "clear":
            await self.sim.clear()
            return
        raise ValueError(f"不支援的動作: {action}")

    async def close(self) -> None:

        if self.sim is not None:
            try:
                await self.sim.close()
            except Exception:
                pass
            self.connected = False
        self.sim = None

        if self.dvt is not None:
            try:
                await self.dvt.close()
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
                await self.lockdown.close()
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


async def _apply_location_action(action: str, lat: float = None, lng: float = None) -> None:
    try:
        await _session.apply(action, lat, lng)
    except Exception:
        await _session.close()
        await _session.apply(action, lat, lng)


def set_location(lat: float, lng: float) -> None:
    global _WEB_LOCATION_SET
    with _WEB_ACTION_LOCK:
        _run_async(_apply_location_action("set", lat, lng))
    _WEB_LOCATION_SET = True


def clear_location() -> None:
    global _WEB_LOCATION_SET
    with _WEB_ACTION_LOCK:
        _run_async(_apply_location_action("clear"))
    _WEB_LOCATION_SET = False


def get_session_status() -> dict:
    return _session.status()


def safe_clear_on_shutdown() -> None:
    global _WEB_LOCATION_SET
    if not _WEB_LOCATION_SET:
        try:
            with _WEB_ACTION_LOCK:
                _run_async(_session.close())
        except Exception:
            pass
        return

    try:
        with _WEB_ACTION_LOCK:
            _run_async(_apply_location_action("clear"))
            _run_async(_session.close())
        logger.info("Web 模式結束前已恢復真實位置。")
    except Exception as e:
        logger.error(f"Web 模式關閉時恢復位置失敗: {e}")
    finally:
        _WEB_LOCATION_SET = False
