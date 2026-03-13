import atexit
import logging
import os
import signal
import sys

from app.factory import create_app
from app.api.routes import safe_shutdown


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _handle_termination_signal(signum, frame):
    try:
        safe_shutdown()
    except Exception as e:
        logger.error(f"結束前清理失敗: {e}")
    logger.info(f"收到結束訊號 ({signum})，正在停止模擬...")
    raise KeyboardInterrupt


if __name__ == "__main__":
    host = os.environ.get("SIMPLE_LOCATION_HOST", "127.0.0.1")
    port = int(os.environ.get("SIMPLE_LOCATION_PORT", "8000"))

    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    signal.signal(signal.SIGINT, _handle_termination_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_termination_signal)

    atexit.register(safe_shutdown)
    app = create_app()
    logger.info(f"Web 模式啟動: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
