"""Звуковые бэкенды. Каждый раз в 20 мс зовёт `engine.process(rx) -> tx` и `notify()`.

AUDIO_DEVICE:
  fake             — синтетический приём (пачки «речи» раз в 8 с), TX пишется в WAV
  fake:<path.wav>  — приём из WAV (8 кГц, моно, 16 бит) по кругу, TX в WAV
  sd:<имя>         — реальное устройство через sounddevice/PortAudio; <имя> — подстрока
                     имени устройства (`python -m sounddevice` покажет список) или
                     `sd:<вход>,<выход>`. Для ALSA-устройств без 8 кГц — PA_ALSA_PLUGHW=1.

TX_DRY_RUN=1 на реальном устройстве: выход — цифровая тишина, передача пишется в WAV.
"""
import asyncio
import logging
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import FRAME, FRAME_MS, RATE
from .engine import IDLE, Engine

log = logging.getLogger(__name__)


class TxRecorder:
    """Пишет каждую передачу (от ptt_down до возврата в idle) в отдельный WAV."""

    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self._wav: wave.Wave_write | None = None
        self.files: list[Path] = []

    def feed(self, frame: np.ndarray, active: bool) -> None:
        if active and self._wav is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / time.strftime("tx-%Y%m%d-%H%M%S.wav")
            self._wav = wave.open(str(path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(RATE)
            self.files.append(path)
            log.info("TX → %s", path)
        if self._wav is not None:
            self._wav.writeframes((np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes())
            if not active:
                self.close()

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


def synthetic_rx(seconds: float = 8.0, burst: float = 3.0) -> np.ndarray:
    """Фон −80 dBFS и раз в `seconds` пачка «речи» −25 dBFS: тон 600–900 Гц с вибрато."""
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    rng = np.random.default_rng(1)
    sig = rng.normal(0, 10 ** (-80 / 20), n)
    on = t < burst
    freq = 750 + 150 * np.sin(2 * np.pi * 3 * t)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
    sig[on] += (10 ** (-25 / 20) * np.sqrt(2) * envelope * np.sin(phase))[on]
    return sig.astype(np.float32)


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path}: нужен WAV 8000 Гц, моно, 16 бит")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


class FakeBackend:
    def __init__(self, engine: Engine, notify: Callable[[], None], data_dir: Path, wav: str | None):
        self.engine, self.notify = engine, notify
        self.rx = load_wav(wav) if wav else synthetic_rx()
        self.recorder = TxRecorder(data_dir)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        pos, period = 0, FRAME_MS / 1000
        next_t = time.monotonic()
        while True:
            chunk = self.rx[pos:pos + FRAME]
            if len(chunk) < FRAME:
                chunk = np.concatenate([chunk, self.rx[:FRAME - len(chunk)]])
            pos = (pos + FRAME) % len(self.rx)
            out = self.engine.process(chunk)
            self.recorder.feed(out, self.engine.tx_state != IDLE)
            self.notify()
            next_t += period
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.recorder.close()


class SoundDeviceBackend:
    def __init__(self, engine: Engine, notify: Callable[[], None], spec: str, data_dir: Path, dry_run: bool):
        import sounddevice as sd
        self.sd = sd
        self.engine, self.notify = engine, notify
        parts = [p.strip() for p in spec.split(",")]
        self.device = (parts[0], parts[-1])
        self.dry_run = dry_run
        self.recorder = TxRecorder(data_dir) if dry_run else None
        self.stream = None

    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.warning("audio: %s", status)
        out = self.engine.process(indata[:, 0])
        if self.dry_run:
            self.recorder.feed(out, self.engine.tx_state != IDLE)
            outdata.fill(0)
        else:
            outdata[:, 0] = out
        self.notify()

    async def start(self) -> None:
        self.stream = self.sd.Stream(
            samplerate=RATE, blocksize=FRAME, channels=1, dtype="float32",
            device=self.device, callback=self._callback, latency="low",
        )
        self.stream.start()
        log.info("audio: %s @ %d Гц%s", self.device, RATE, " (TX_DRY_RUN)" if self.dry_run else "")

    async def stop(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()
        if self.recorder:
            self.recorder.close()


def make_backend(spec: str, engine: Engine, notify: Callable[[], None], data_dir: Path, dry_run: bool):
    if spec == "fake" or spec.startswith("fake:"):
        return FakeBackend(engine, notify, data_dir, spec[5:] or None)
    if spec.startswith("sd:"):
        return SoundDeviceBackend(engine, notify, spec[3:], data_dir, dry_run)
    raise ValueError(f"AUDIO_DEVICE={spec!r}: ожидается fake, fake:<wav> или sd:<устройство>")
