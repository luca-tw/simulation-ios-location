import argparse
import logging
import time
import asyncio
import sys
import os
import signal
import traceback
import threading
import queue
from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.cli.mounter import auto_mount
from pymobiledevice3.service_connection import ServiceConnection

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

async def start_simulation(lat, lng):
    devices = list_devices()
    if not devices:
        logger.error("未找到 iOS 裝置。請確認已連接並信任電腦。")
        return

    device = devices[0]
    udid = device.serial
    logger.info(f"發現裝置: {udid}")

    lockdown = None
    try:
        # 1. 建立 Lockdown 連線
        lockdown = create_using_usbmux(udid)
        ios_version = lockdown.product_version
        logger.info(f"iOS 版本: {ios_version}")

        # 2. 自動掛載 Developer Disk Image
        logger.info("檢查並掛載 Developer Disk Image...")
        try:
            await auto_mount(lockdown)
            logger.info("Image 掛載成功 (或已掛載)")
        except Exception as e:
            logger.warning(f"掛載 Image 時發生警告 (可能已掛載): {e}")

        major_version = int(ios_version.split('.')[0])
        
        if major_version >= 17:
             # iOS 17+ 流程: RSD -> Tunnel -> DVT
            if sys.platform == "darwin" and os.geteuid() != 0:
                logger.error("iOS 17+ 在 macOS 需要 root 權限建立 Tunnel。請改用: sudo python simple_location.py --lat <緯度> --lng <經度>")
                return
            logger.info("檢測到 iOS 17+，正在嘗試建立 Tunnel...")
            
            try:
                # Tunnel 需在事件迴圈中持續轉送封包；改由背景執行緒維持，避免被同步 DVT 連線阻塞
                result_queue: queue.Queue = queue.Queue()
                stop_event = threading.Event()
                tunnel_thread = threading.Thread(
                    target=_run_tunnel_thread,
                    args=(udid, result_queue, stop_event),
                    daemon=True,
                )
                tunnel_thread.start()

                try:
                    result = result_queue.get(timeout=20)
                except queue.Empty as e:
                    raise TimeoutError("等待 Tunnel 建立逾時") from e

                if result[0] == "error":
                    raise RuntimeError(f"Tunnel 建立失敗: {result[1]}\n{result[2]}")

                host, port = result[1], result[2]
                logger.info(f"Tunnel 已建立: {host}:{port}")

                try:
                    async with RobustRemoteServiceDiscoveryService((host, port)) as sp_rsd:
                        logger.info("正在建立 DVT 連線...")
                        for attempt in range(1, 4):
                            try:
                                with DvtSecureSocketProxyService(sp_rsd) as dvt:
                                    perform_simulation(dvt, lat, lng)
                                break
                            except TimeoutError as e:
                                logger.warning(f"DVT 連線逾時 (第 {attempt}/3 次): {e}")
                                if attempt < 3:
                                    await asyncio.sleep(1)
                                else:
                                    raise
                finally:
                    stop_event.set()
                    tunnel_thread.join(timeout=2)
            except Exception as e:
                logger.error(f"iOS 17 Tunnel 連線失敗: {e}")
                traceback.print_exc()
                logger.info("嘗試使用標準 Lockdown 連線...")
                try:
                    with DvtSecureSocketProxyService(lockdown=lockdown) as dvt:
                        perform_simulation(dvt, lat, lng)
                except Exception as e2:
                    logger.error(f"標準連線也失敗: {e2}")

        else:
            # iOS 16 以下流程: Lockdown -> DVT
            logger.info("檢測到 iOS 16 以下，使用標準 Lockdown 連線...")
            with DvtSecureSocketProxyService(lockdown=lockdown) as dvt:
                perform_simulation(dvt, lat, lng)

    except Exception as e:
        logger.error(f"發生錯誤: {e}")
        traceback.print_exc()

def perform_simulation(dvt_service, lat, lng):
    """
    執行位置模擬邏輯
    """
    sim = None
    location_set = False
    try:
        sim = LocationSimulation(dvt_service)
        logger.info(f"正在設定位置到: {lat}, {lng}")
        sim.set(lat, lng)
        location_set = True
        logger.info("位置已更新！ (請在手機地圖確認)")
        logger.info("請保持此視窗開啟。按 Ctrl+C 停止模擬並恢復位置...")
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("停止模擬...")
    except Exception as e:
        logger.error(f"模擬過程中發生錯誤: {e}")
    finally:
        # 不論正常結束、Ctrl+C 或其他例外，都盡量清除模擬位置
        if sim is not None and location_set:
            try:
                sim.clear()
                logger.info("位置已恢復正常。")
            except Exception as e:
                logger.error(f"恢復位置時發生錯誤: {e}")


def _handle_termination_signal(signum, frame):
    logger.info(f"收到結束訊號 ({signum})，正在停止模擬...")
    raise KeyboardInterrupt

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="簡易 iOS 位置模擬器")
    parser.add_argument("--lat", type=float, default=25.033964, help="緯度 (預設: 台北 101)")
    parser.add_argument("--lng", type=float, default=121.564468, help="經度 (預設: 台北 101)")
    
    args = parser.parse_args()
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 讓 Ctrl+C / SIGTERM 都能走到清理流程 (sim.clear)
    signal.signal(signal.SIGINT, _handle_termination_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_termination_signal)
    
    # 執行 main loop
    try:
        asyncio.run(start_simulation(args.lat, args.lng))
    except KeyboardInterrupt:
        pass
