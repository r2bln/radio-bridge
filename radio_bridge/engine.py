"""Ядро: автомат передачи и гейт приёма. Без сети и без звуковых устройств.

Звуковой бэкенд раз в кадр (20 мс) вызывает `process(rx_frame) -> tx_frame`.
Всё время в движке считается в кадрах — поэтому он детерминирован и тестируется
без часов. Сетевые события (PTT, голос клиента) приходят из другого потока, так что
все публичные методы под одной блокировкой.

Инвариант: выход формируется только из тона VOX и голоса клиента. Вход (приём) в
выход не попадает никогда — он уходит только в события `rx`.
"""
import math
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from . import FRAME, FRAME_MS, RATE, ulaw
from .config import Config

IDLE, WAKE, VOICE, LOCKED = "idle", "wake", "voice", "locked"
SILENCE = np.zeros(FRAME, dtype=np.float32)
LEVEL_EVERY = 5          # кадров между событиями level (≈10 раз/с)
STATE_TICK = 50          # кадров между повторами state во время TX (1 с)
FADE = int(0.010 * RATE)  # фронты тона 10 мс


def _frames(ms: float) -> int:
    return max(1, int(round(ms / FRAME_MS)))


def dbfs(x: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20 * math.log10(rms) if rms > 1e-10 else -120.0


@dataclass
class Event:
    kind: str            # "rx" | "state" | "level" | "tot_expired"
    data: object = None  # bytes для rx, dict для остальных


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._events: list[Event] = []

        self.tx_state = IDLE
        self._frame_no = 0
        self._tx_start = 0
        self._last_voice = 0
        self._tail_until = -1
        self._voice: deque[np.ndarray] = deque(maxlen=_frames(cfg.tx_max_buffer_ms))
        self._tx_gain = 10 ** (cfg.tx_gain_db / 20)
        self._tx_level = -120.0

        self._wake_frames = _frames(cfg.vox_tone_ms)
        self._watchdog = _frames(cfg.tx_watchdog_ms)
        self._tail = _frames(cfg.tx_tail_ms)
        self._tot = int(cfg.tx_tot_s * 1000 / FRAME_MS)
        self._tone = self._make_tone()

        self.rx_open = False
        self._rx_gain = 10 ** (cfg.rx_gain_db / 20)
        self._hang = _frames(cfg.rx_hang_ms)
        self._quiet = 0
        self._preroll: deque[np.ndarray] = deque(maxlen=_frames(cfg.rx_preroll_ms))
        self._rx_level = -120.0

    # ---- события из сети -------------------------------------------------

    def ptt_down(self) -> None:
        with self._lock:
            if self.tx_state != IDLE:
                return  # в LOCKED — только через ptt_up
            self._voice.clear()
            self._tx_start = self._last_voice = self._frame_no
            self._set_tx(WAKE)

    def ptt_up(self) -> None:
        with self._lock:
            if self.tx_state in (WAKE, VOICE):
                self._tail_until = self._frame_no + self._tail
            if self.tx_state != IDLE:
                self._voice.clear()
                self._set_tx(IDLE)

    def push_voice(self, data: bytes) -> None:
        if len(data) != FRAME:
            return
        with self._lock:
            if self.tx_state not in (WAKE, VOICE):
                return  # вне передачи голос клиента выбрасывается
            frame = np.clip(ulaw.decode(data) * self._tx_gain, -1.0, 1.0)
            self._tx_level = dbfs(frame)
            self._voice.append(frame)
            self._last_voice = self._frame_no

    def client_gone(self) -> None:
        """Клиент отключился: передача прекращается немедленно."""
        with self._lock:
            if self.tx_state in (WAKE, VOICE):
                self._tail_until = self._frame_no + self._tail
            self._voice.clear()
            if self.tx_state != IDLE:
                self._set_tx(IDLE)

    def drain_events(self) -> list[Event]:
        with self._lock:
            ev, self._events = self._events, []
            return ev

    def snapshot(self) -> dict:
        with self._lock:
            return self._state_dict()

    # ---- кадр звукового бэкенда -----------------------------------------

    def process(self, rx: np.ndarray) -> np.ndarray:
        with self._lock:
            out = self._tx_step()
            self._rx_step(np.asarray(rx, dtype=np.float32))
            if self._frame_no % LEVEL_EVERY == 0:
                tx_db = self._tx_level if self.tx_state in (WAKE, VOICE) else -120.0
                self._emit("level", {"rx_dbfs": round(self._rx_level, 1), "tx_dbfs": round(tx_db, 1)})
            elapsed = self._frame_no - self._tx_start
            if self.tx_state in (WAKE, VOICE, LOCKED) and elapsed > 0 and elapsed % STATE_TICK == 0:
                self._emit_state()
            self._frame_no += 1
            return out

    # ---- внутреннее ------------------------------------------------------

    def _tx_step(self) -> np.ndarray:
        st = self.tx_state
        if st in (IDLE, LOCKED):
            return SILENCE
        elapsed = self._frame_no - self._tx_start
        if elapsed >= self._tot:
            self._lockout()
            self._emit("tot_expired", {"tot_s": self.cfg.tx_tot_s})
            return SILENCE
        if self._frame_no - self._last_voice > self._watchdog:
            self._lockout()
            return SILENCE
        if st == WAKE:
            if elapsed < self._wake_frames:
                return self._tone[elapsed]
            self._set_tx(VOICE)
        return self._voice.popleft() if self._voice else SILENCE

    def _lockout(self) -> None:
        self._voice.clear()
        self._tail_until = self._frame_no + self._tail
        self._set_tx(LOCKED)

    def _rx_step(self, rx: np.ndarray) -> None:
        self._rx_level = dbfs(rx)
        suppressed = self.tx_state in (WAKE, VOICE) or self._frame_no < self._tail_until
        if suppressed:
            self._preroll.clear()
            if self.rx_open:
                self.rx_open = False
                self._emit_state()
            return
        frame = np.tanh(rx * self._rx_gain).astype(np.float32)  # мягкий ограничитель пиков
        loud = self._rx_level > self.cfg.rx_gate_db
        if not self.rx_open:
            if loud:
                self.rx_open = True
                self._quiet = 0
                self._emit_state()
                for f in self._preroll:
                    self._emit("rx", ulaw.encode(f))
                self._preroll.clear()
                self._emit("rx", ulaw.encode(frame))
            else:
                self._preroll.append(frame)
            return
        self._emit("rx", ulaw.encode(frame))
        self._quiet = 0 if loud else self._quiet + 1
        if self._quiet >= self._hang:
            self.rx_open = False
            self._emit_state()

    def _make_tone(self) -> list[np.ndarray]:
        n = self._wake_frames * FRAME
        t = np.arange(n) / RATE
        amp = 10 ** (self.cfg.vox_tone_dbfs / 20)
        env = np.minimum(1.0, np.minimum(np.arange(n), n - 1 - np.arange(n)) / FADE)
        tone = (amp * env * np.sin(2 * np.pi * self.cfg.vox_tone_hz * t)).astype(np.float32)
        return [tone[i * FRAME:(i + 1) * FRAME] for i in range(self._wake_frames)]

    def _set_tx(self, st: str) -> None:
        if st != self.tx_state:
            self.tx_state = st
            if st not in (WAKE, VOICE):
                self._tx_level = -120.0
            self._emit_state()

    def _state_dict(self) -> dict:
        active = self.tx_state in (WAKE, VOICE, LOCKED)
        elapsed = (self._frame_no - self._tx_start) * FRAME_MS / 1000 if active else 0.0
        return {"tx": self.tx_state, "tx_elapsed_s": round(elapsed, 2), "rx_open": self.rx_open}

    def _emit_state(self) -> None:
        self._emit("state", self._state_dict())

    def _emit(self, kind: str, data=None) -> None:
        self._events.append(Event(kind, data))
