"""G.711 μ-law на numpy (audioop в Python 3.13 удалён).

Сэмплы — float32 в [-1, 1]; внутри кодек работает с 14-битной шкалой G.711.
"""
import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    pcm = np.where(sign, -magnitude, magnitude)
    return (pcm / 32768.0).astype(np.float32)


_DECODE = _build_decode_table()


def encode(x: np.ndarray) -> bytes:
    pcm = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32768.0), -32768, 32767).astype(np.int32)
    sign = np.where(pcm < 0, 0x80, 0)
    mag = np.minimum(np.abs(pcm), _CLIP) + _BIAS
    # exponent = позиция старшего бита в mag >> 7 (0..7)
    exponent = np.floor(np.log2(np.maximum(mag >> 7, 1))).astype(np.int32)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    u = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return u.astype(np.uint8).tobytes()


def decode(data: bytes) -> np.ndarray:
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]
