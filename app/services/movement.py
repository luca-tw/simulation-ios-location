import asyncio
import logging
import math
import time

from app.services import location

logger = logging.getLogger(__name__)

SPEED_KMH = {
    "walk": 6.0,
    "bike": 18.0,
    "scooter": 60.0,
    "car": 120.0,
    "hsr": 230.0,
}

TICK_SECONDS = 0.2
_EARTH_R_M = 6_371_000.0


def _speed_mps(mode: str) -> float:
    return SPEED_KMH.get(mode, 0.0) * 1000.0 / 3600.0


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _step_toward(lat: float, lng: float, target_lat: float, target_lng: float, step_m: float) -> tuple:
    remaining = _haversine_m(lat, lng, target_lat, target_lng)
    if remaining <= step_m or remaining == 0:
        return target_lat, target_lng, True
    ratio = step_m / remaining
    return lat + (target_lat - lat) * ratio, lng + (target_lng - lng) * ratio, False


class _MovementState:
    def __init__(self) -> None:
        self.mode: str = "idle"  # idle | moving | playing_route
        self.move_type: str = "instant"
        self.current_lat: float = None
        self.current_lng: float = None
        self.target_lat: float = None
        self.target_lng: float = None
        self.route_points: list = []
        self.route_index: int = 0
        self.last_error: str = None
        self.task: asyncio.Task = None
        self._lock: asyncio.Lock = None

    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "move_type": self.move_type,
            "current": None if self.current_lat is None else {"lat": self.current_lat, "lng": self.current_lng},
            "target": None if self.target_lat is None else {"lat": self.target_lat, "lng": self.target_lng},
            "route_length": len(self.route_points),
            "route_index": self.route_index,
            "last_error": self.last_error,
        }


_state = _MovementState()


async def _apply_to_device(lat: float, lng: float) -> None:
    await location._session.apply_when_connected("set", lat, lng)


async def _cancel_current_task() -> None:
    task = _state.task
    if task is None or task.done():
        _state.task = None
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    _state.task = None


async def _run_move_to(target_lat: float, target_lng: float, move_type: str) -> None:
    speed = _speed_mps(move_type)
    last = time.monotonic()
    try:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now = time.monotonic()
            dt = now - last
            last = now

            if _state.current_lat is None:
                _state.current_lat, _state.current_lng = target_lat, target_lng

            step = speed * dt
            new_lat, new_lng, arrived = _step_toward(
                _state.current_lat, _state.current_lng, target_lat, target_lng, step
            )
            async with _state.lock():
                await _apply_to_device(new_lat, new_lng)
            _state.current_lat, _state.current_lng = new_lat, new_lng

            if arrived:
                _state.mode = "idle"
                _state.target_lat = None
                _state.target_lng = None
                return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"移動 task 失敗: {e}")
        _state.last_error = str(e)
        _state.mode = "idle"


async def _run_play_route(points: list, move_type: str) -> None:
    speed = _speed_mps(move_type)
    last = time.monotonic()

    if _state.current_lat is None and points:
        _state.current_lat, _state.current_lng = points[0]["lat"], points[0]["lng"]
        try:
            async with _state.lock():
                await _apply_to_device(_state.current_lat, _state.current_lng)
        except Exception as e:
            logger.error(f"路徑起始設定失敗: {e}")
            _state.last_error = str(e)
            _state.mode = "idle"
            return

    try:
        while _state.route_index < len(points):
            target = points[_state.route_index]
            target_lat, target_lng = target["lat"], target["lng"]
            _state.target_lat, _state.target_lng = target_lat, target_lng

            while True:
                await asyncio.sleep(TICK_SECONDS)
                now = time.monotonic()
                dt = now - last
                last = now

                step = speed * dt
                new_lat, new_lng, arrived = _step_toward(
                    _state.current_lat, _state.current_lng, target_lat, target_lng, step
                )
                async with _state.lock():
                    await _apply_to_device(new_lat, new_lng)
                _state.current_lat, _state.current_lng = new_lat, new_lng
                if arrived:
                    break

            _state.route_index += 1

        _state.mode = "idle"
        _state.target_lat = None
        _state.target_lng = None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"路徑播放 task 失敗: {e}")
        _state.last_error = str(e)
        _state.mode = "idle"


async def _start_move_coro(lat: float, lng: float, move_type: str) -> None:
    await _cancel_current_task()
    _state.last_error = None
    _state.move_type = move_type

    if move_type == "instant" or _state.current_lat is None:
        async with _state.lock():
            await _apply_to_device(lat, lng)
        _state.current_lat, _state.current_lng = lat, lng
        if move_type == "instant":
            _state.mode = "idle"
            _state.target_lat = None
            _state.target_lng = None
            return

    _state.mode = "moving"
    _state.target_lat, _state.target_lng = lat, lng
    _state.task = asyncio.create_task(_run_move_to(lat, lng, move_type))


async def _start_route_coro(points: list, move_type: str) -> None:
    await _cancel_current_task()
    _state.last_error = None
    _state.move_type = move_type
    _state.route_points = points
    _state.route_index = 0
    _state.mode = "playing_route"

    if move_type == "instant":
        if points:
            last = points[-1]
            async with _state.lock():
                await _apply_to_device(last["lat"], last["lng"])
            _state.current_lat, _state.current_lng = last["lat"], last["lng"]
            _state.route_index = len(points)
        _state.mode = "idle"
        return

    _state.task = asyncio.create_task(_run_play_route(points, move_type))


async def _stop_coro() -> None:
    await _cancel_current_task()
    _state.mode = "idle"
    _state.target_lat = None
    _state.target_lng = None


async def _set_instant_coro(lat: float, lng: float) -> None:
    await _cancel_current_task()
    async with _state.lock():
        await _apply_to_device(lat, lng)
    _state.current_lat, _state.current_lng = lat, lng
    _state.mode = "idle"
    _state.target_lat = None
    _state.target_lng = None
    _state.last_error = None


def start_move(lat: float, lng: float, move_type: str) -> dict:
    if move_type not in ("instant",) and move_type not in SPEED_KMH:
        raise ValueError(f"不支援的移動模式: {move_type}")
    location._run_async(_start_move_coro(lat, lng, move_type))
    return _state.snapshot()


def start_route(points: list, move_type: str) -> dict:
    if move_type not in ("instant",) and move_type not in SPEED_KMH:
        raise ValueError(f"不支援的移動模式: {move_type}")
    cleaned = []
    for p in points or []:
        try:
            cleaned.append({"lat": float(p["lat"]), "lng": float(p["lng"])})
        except (TypeError, ValueError, KeyError):
            continue
    if len(cleaned) < 2:
        raise ValueError("路徑至少需要 2 個點")
    location._run_async(_start_route_coro(cleaned, move_type))
    return _state.snapshot()


def stop() -> dict:
    location._run_async(_stop_coro())
    return _state.snapshot()


def set_instant(lat: float, lng: float) -> dict:
    location._run_async(_set_instant_coro(lat, lng))
    return _state.snapshot()


def status() -> dict:
    return _state.snapshot()
