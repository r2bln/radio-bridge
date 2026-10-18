import numpy as np

from radio_bridge import ulaw


def test_known_codes():
    # G.711: ноль кодируется как 0xFF, крайние значения — 0x80 / 0x00
    assert ulaw.encode(np.array([0.0])) == b"\xff"
    assert ulaw.encode(np.array([1.0])) == b"\x80"
    assert ulaw.encode(np.array([-1.0])) == b"\x00"
    assert ulaw.decode(b"\xff")[0] == 0.0


def test_all_codes_roundtrip():
    codes = bytes(range(256))
    # 0x7F и 0xFF оба декодируются в ноль — обратно получится 0xFF
    expected = codes.replace(b"\x7f", b"\xff")
    assert ulaw.encode(ulaw.decode(codes)) == expected


def test_roundtrip_error_is_small_relative():
    x = np.linspace(-0.9, 0.9, 2001)
    y = ulaw.decode(ulaw.encode(x))
    big = np.abs(x) > 0.01
    assert np.max(np.abs(y[big] - x[big]) / np.abs(x[big])) < 0.07
    assert np.max(np.abs(y - x)) < 0.02
