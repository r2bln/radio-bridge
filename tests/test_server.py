import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from radio_bridge import ulaw
from radio_bridge.config import Config
from radio_bridge.server import CLOSE_BUSY, build_app


def run(coro):
    return asyncio.run(coro)


async def _client(tmp_path):
    cfg = Config(audio_device="fake", tls=False, data_dir=tmp_path, tx_tot_s=5, vox_tone_ms=100)
    client = TestClient(TestServer(build_app(cfg)))
    await client.start_server()
    return client


async def _recv_json(ws, type_, timeout=3.0):
    async with asyncio.timeout(timeout):
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data["type"] == type_:
                    return data


def test_index_served(tmp_path):
    async def go():
        c = await _client(tmp_path)
        try:
            r = await c.get("/")
            assert r.status == 200 and "PTT" in await r.text()
            r = await c.get("/static/app.js")
            assert r.status == 200
        finally:
            await c.close()
    run(go())


def test_hello_and_second_client_busy(tmp_path):
    async def go():
        c = await _client(tmp_path)
        try:
            ws1 = await c.ws_connect("/ws")
            hello = await _recv_json(ws1, "hello")
            assert hello["version"] == 1 and hello["tot_s"] == 5 and hello["rate"] == 8000
            ws2 = await c.ws_connect("/ws")
            msg = await ws2.receive()
            assert msg.type == aiohttp.WSMsgType.CLOSE and msg.data == CLOSE_BUSY
            await ws1.close()
            await asyncio.sleep(0.1)
            ws3 = await c.ws_connect("/ws")  # после ухода первого — свободно
            assert (await _recv_json(ws3, "hello"))["version"] == 1
            await ws3.close()
        finally:
            await c.close()
    run(go())


def test_ptt_cycle_writes_tx_wav(tmp_path):
    async def go():
        c = await _client(tmp_path)
        try:
            ws = await c.ws_connect("/ws")
            await _recv_json(ws, "hello")
            await ws.send_str(json.dumps({"type": "ptt", "state": "down"}))
            st = await _recv_json(ws, "state")
            while st["tx"] != "wake":
                st = await _recv_json(ws, "state")
            import numpy as np
            frame = ulaw.encode(0.3 * np.sin(np.arange(160) * 0.6))
            for _ in range(20):
                await ws.send_bytes(frame)
                await asyncio.sleep(0.02)
            await ws.send_str(json.dumps({"type": "ptt", "state": "up"}))
            st = await _recv_json(ws, "state")
            while st["tx"] != "idle":
                st = await _recv_json(ws, "state")
            await ws.close()
        finally:
            await c.close()
        wavs = list(tmp_path.glob("tx-*.wav"))
        assert len(wavs) == 1 and wavs[0].stat().st_size > 44 + 2 * 8000 * 0.2
    run(go())


def test_disconnect_during_tx_releases(tmp_path):
    async def go():
        c = await _client(tmp_path)
        try:
            ws = await c.ws_connect("/ws")
            await _recv_json(ws, "hello")
            await ws.send_str(json.dumps({"type": "ptt", "state": "down"}))
            await asyncio.sleep(0.1)
            await ws.close()
            await asyncio.sleep(0.1)
            from radio_bridge.server import ENGINE
            assert c.server.app[ENGINE].tx_state == "idle"
        finally:
            await c.close()
    run(go())
