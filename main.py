import atexit
import logging
import os
import signal
import socket
import sys
import threading

from app.factory import create_app
from app.api.routes import safe_shutdown


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


_shutdown_done = threading.Event()


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _run_shutdown_once():
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        safe_shutdown()
        logger.info("已完成關閉清理")
    except BaseException as e:
        logger.error(f"結束前清理失敗: {e}")


if __name__ == "__main__":
    host = os.environ.get("SIMPLE_LOCATION_HOST", "127.0.0.1")
    port = int(os.environ.get("SIMPLE_LOCATION_PORT", "8000"))

    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    atexit.register(_run_shutdown_once)
    app = create_app()
    logger.info(f"Web 模式啟動: http://{host}:{port}")
    if host in ("0.0.0.0", "::"):
        lan_ip = _lan_ip()
        if lan_ip:
            logger.info(f"內網裝置可透過 http://{lan_ip}:{port} 存取")
    try:
        app.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，停止中...")
    finally:
        _run_shutdown_once()
        os._exit(0)
