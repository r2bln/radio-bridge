"""Кодировщик SSTV: RGB-картинка → звук 8 кГц (float32). Режимы Robot 36, Martin M1, Scottie S1.

Сигнал собирается из отрезков (частота, длительность); границы отрезков округляются до
сэмпла независимо друг от друга (без накопления ошибки), фаза непрерывная.
Пиксель: 1500 Гц (чёрный) … 2300 Гц (белый), синхро 1200 Гц.
"""
from dataclasses import dataclass

import numpy as np

from . import RATE

BLACK, WHITE, SYNC = 1500.0, 2300.0, 1200.0


@dataclass(frozen=True)
class Mode:
    name: str
    title: str
    vis: int
    width: int
    height: int


MODES = {
    "robot36": Mode("robot36", "Robot 36", 8, 320, 240),
    "martin1": Mode("martin1", "Martin M1", 44, 320, 256),
    "scottie1": Mode("scottie1", "Scottie S1", 60, 320, 256),
}


def _pix(values: np.ndarray) -> np.ndarray:
    return BLACK + np.asarray(values, dtype=np.float64) * (WHITE - BLACK) / 255.0


class _Seq:
    """Накопитель отрезков: частоты и длительности в мс."""

    def __init__(self):
        self.f: list[np.ndarray] = []
        self.d: list[np.ndarray] = []

    def tone(self, freq: float, ms: float) -> None:
        self.f.append(np.array([freq]))
        self.d.append(np.array([ms]))

    def scan(self, values: np.ndarray, ms_total: float) -> None:
        self.f.append(_pix(values))
        self.d.append(np.full(len(values), ms_total / len(values)))

    def render(self, amp: float) -> np.ndarray:
        f = np.concatenate(self.f)
        ends = np.round(np.cumsum(np.concatenate(self.d)) / 1000.0 * RATE).astype(np.int64)
        counts = np.diff(np.concatenate([[0], ends]))
        inst = np.repeat(f, counts)
        phase = 2 * np.pi * np.cumsum(inst) / RATE
        return (amp * np.sin(phase)).astype(np.float32)


def _vis(seq: _Seq, code: int) -> None:
    seq.tone(1900, 300)
    seq.tone(1200, 10)
    seq.tone(1900, 300)
    seq.tone(1200, 30)                       # старт-бит
    bits = [(code >> i) & 1 for i in range(7)]
    for b in bits + [sum(bits) % 2]:         # LSB первым, затем чётность (even)
        seq.tone(1100 if b else 1300, 30)
    seq.tone(1200, 30)                       # стоп-бит


def _robot36(seq: _Seq, rgb: np.ndarray) -> None:
    r, g, b = (rgb[..., i].astype(np.float64) for i in range(3))
    y = 16 + (65.738 * r + 129.057 * g + 25.064 * b) / 256
    u = 128 + (-37.945 * r - 74.494 * g + 112.439 * b) / 256   # B-Y
    v = 128 + (112.439 * r - 94.154 * g - 18.285 * b) / 256    # R-Y
    for line in range(rgb.shape[0]):
        pair = slice(line - line % 2, line - line % 2 + 2)     # цветность 4:2:0 — среднее по паре строк
        seq.tone(SYNC, 9)
        seq.tone(BLACK, 3)
        seq.scan(np.clip(y[line], 0, 255), 88)
        if line % 2 == 0:
            seq.tone(BLACK, 4.5)
            seq.tone(1900, 1.5)
            seq.scan(np.clip(v[pair].mean(axis=0), 0, 255), 44)
        else:
            seq.tone(WHITE, 4.5)
            seq.tone(1900, 1.5)
            seq.scan(np.clip(u[pair].mean(axis=0), 0, 255), 44)


def _martin1(seq: _Seq, rgb: np.ndarray) -> None:
    scan, sep = 146.432, 0.572
    for line in rgb:
        seq.tone(SYNC, 4.862)
        seq.tone(BLACK, sep)
        for ch in (1, 2, 0):                 # G, B, R
            seq.scan(line[:, ch], scan)
            seq.tone(BLACK, sep)


def _scottie1(seq: _Seq, rgb: np.ndarray) -> None:
    scan, sep = 138.240, 1.5
    seq.tone(SYNC, 9)                        # стартовый синхроимпульс
    for line in rgb:
        seq.tone(BLACK, sep)
        seq.scan(line[:, 1], scan)           # G
        seq.tone(BLACK, sep)
        seq.scan(line[:, 2], scan)           # B
        seq.tone(SYNC, 9)
        seq.tone(BLACK, sep)
        seq.scan(line[:, 0], scan)           # R


_ENCODERS = {"robot36": _robot36, "martin1": _martin1, "scottie1": _scottie1}


def encode(mode: str, rgb: np.ndarray, dbfs: float = -12.0) -> np.ndarray:
    """rgb: uint8 массив (height, width, 3) ровно под размер режима."""
    m = MODES[mode]
    if rgb.shape != (m.height, m.width, 3):
        raise ValueError(f"{m.title}: нужна картинка {m.width}×{m.height}, пришла {rgb.shape[1]}×{rgb.shape[0]}")
    seq = _Seq()
    _vis(seq, m.vis)
    _ENCODERS[mode](seq, rgb)
    seq.tone(BLACK, 300)                     # хвост, чтобы декодер дорисовал последнюю строку
    return seq.render(10 ** (dbfs / 20))
