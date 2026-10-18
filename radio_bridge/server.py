"""HTTP(S) + WebSocket: отдаёт страницу клиента и гоняет звук/PTT (протокол — docs/SPEC.md §6)."""
import asyncio
import json
import logging
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from . import FRAME, FRAME_MS, PROTOCOL_VERSION, RATE, __version__
from .audio import make_backend
from .config import Config
from .engine import Engine

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
CLOSE_BUSY = 4001

ENGINE = web.AppKey("engine", Engine)
CONFIG = web.AppKey("config", Config)


class Hub:
    """Единственный клиент + перекачка событий движка в его WebSocket."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.ws: web.WebSocketResponse | None = None
        self._wake = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    def notify(self) -> None:
        """Зовётся из звукового потока после каждого кадра."""
        self._loop.call_soon_threadsafe(self._wake.set)

    async def pump(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            events = self.engine.drain_events()
            ws = self.ws
            if ws is None or ws.closed:
                continue
            try:
                for ev in events:
                    if ev.kind == "rx":
                        await ws.send_bytes(ev.data)
                    else:
                        await ws.send_str(json.dumps({"type": ev.kind, **(ev.data or {})}))
            except (ConnectionResetError, RuntimeError) as e:
                log.info("отправка клиенту не удалась: %s", e)


HUB = web.AppKey("hub", Hub)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    hub, engine, cfg = app[HUB], app[ENGINE], app[CONFIG]
    ws = web.WebSocketResponse(heartbeat=5.0, max_msg_size=64 * 1024)
    await ws.prepare(request)
    peer = request.remote
    if hub.ws is not None and not hub.ws.closed:
        log.info("отказ %s: станция занята", peer)
        await ws.close(code=CLOSE_BUSY, message=b"busy")
        return ws

    hub.ws = ws
    log.info("клиент подключился: %s", peer)
    await ws.send_str(json.dumps({
        "type": "hello", "version": PROTOCOL_VERSION, "server": __version__,
        "tot_s": cfg.tx_tot_s, "rate": RATE, "frame_ms": FRAME_MS,
    }))
    await ws.send_str(json.dumps({"type": "state", **engine.snapshot()}))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if len(msg.data) == FRAME:
                    engine.push_voice(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "ptt":
                    if data.get("state") == "down":
                        engine.ptt_down()
                    elif data.get("state") == "up":
                        engine.ptt_up()
            elif msg.type == WSMsgType.ERROR:
                log.warning("ws ошибка: %s", ws.exception())
    finally:
        engine.client_gone()
        if hub.ws is ws:
            hub.ws = None
        log.info("клиент отключился: %s", peer)
    return ws


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


def build_app(cfg: Config) -> web.Application:
    app = web.Application()
    app[CONFIG] = cfg
    app[ENGINE] = Engine(cfg)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC)

    async def lifecycle(app: web.Application):
        hub = Hub(app[ENGINE])
        app[HUB] = hub
        backend = make_backend(cfg.audio_device, app[ENGINE], hub.notify, cfg.data_dir, cfg.tx_dry_run)
        pump = asyncio.create_task(hub.pump())
        await backend.start()
        yield
        await backend.stop()
        pump.cancel()
        if hub.ws is not None:
            await hub.ws.close(code=WSCloseCode.GOING_AWAY, message=b"shutdown")

    app.cleanup_ctx.append(lifecycle)
    return app
