import atexit
import logging
import os
import signal
import sys
import threading

from app.factory import create_app
from app.api.routes import safe_shutdown


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


_shutdown_done = threading.Event()


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
    try:
        app.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，停止中...")
    finally:
        _run_shutdown_once()
        os._exit(0)
