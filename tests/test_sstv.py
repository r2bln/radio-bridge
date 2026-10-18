import numpy as np
import pytest

from radio_bridge import RATE, sstv
from radio_bridge.config import Config
from radio_bridge.engine import IDLE, SSTV, Engine, dbfs

QUIET = np.zeros(160, dtype=np.float32)


def image(mode, color=(255, 255, 255)):
    m = sstv.MODES[mode]
    return np.tile(np.array(color, dtype=np.uint8), (m.height, m.width, 1))


def freq(x, t0_ms, t1_ms):
    """Частота по переходам через ноль на отрезке [t0, t1]."""
    s = x[int(t0_ms * RATE / 1000):int(t1_ms * RATE / 1000)]
    crossings = np.count_nonzero(np.diff(np.signbit(s).astype(np.int8)))
    return crossings / 2 / (len(s) / RATE)


@pytest.mark.parametrize("mode,seconds", [("robot36", 37.21), ("martin1", 115.50), ("scottie1", 110.84)])
def test_durations(mode, seconds):
    x = sstv.encode(mode, image(mode))
    assert len(x) / RATE == pytest.approx(seconds, abs=0.02)


@pytest.mark.parametrize("mode", list(sstv.MODES))
def test_vis_code_decodes(mode):
    x = sstv.encode(mode, image(mode))
    assert freq(x, 20, 280) == pytest.approx(1900, abs=30)
    assert freq(x, 615, 635) == pytest.approx(1200, abs=60)   # старт-бит
    bits = [1 if freq(x, 640 + 30 * i + 5, 640 + 30 * i + 25) < 1200 else 0 for i in range(8)]
    code = sum(b << i for i, b in enumerate(bits[:7]))
    assert code == sstv.MODES[mode].vis
    assert bits[7] == sum(bits[:7]) % 2


def test_martin_pixels_follow_colors():
    x = sstv.encode("martin1", image("martin1", (255, 0, 0)))  # красный: G и B чёрные, R белый
    line0 = 910 + 4.862 + 0.572
    assert freq(x, line0 + 20, line0 + 120) == pytest.approx(1500, abs=40)          # G
    red = line0 + 2 * (146.432 + 0.572)
    assert freq(x, red + 20, red + 120) == pytest.approx(2300, abs=40)              # R


def test_wrong_size_rejected():
    with pytest.raises(ValueError):
        sstv.encode("robot36", np.zeros((10, 10, 3), dtype=np.uint8))


def make_engine(**kw):
    return Engine(Config(**{"vox_tone_ms": 100, "tx_tail_ms": 100, **kw}))


def test_engine_plays_tone_then_sstv_then_idle():
    e = make_engine()
    samples = np.full(160 * 10, 0.25, dtype=np.float32)
    assert e.start_sstv(samples, "robot36") is None
    assert e.tx_state == SSTV
    outs = [e.process(QUIET) for _ in range(5)]          # 100 мс тона
    assert all(dbfs(o) > -20 for o in outs[1:-1])
    outs = [e.process(QUIET) for _ in range(10)]
    assert all(np.allclose(o, 0.25) for o in outs)
    assert not e.process(QUIET).any()
    assert e.tx_state == IDLE


def test_sstv_survives_ptt_up_but_stops_on_stop_and_disconnect():
    for stop in ("stop_sstv", "client_gone"):
        e = make_engine()
        e.start_sstv(np.full(160 * 100, 0.25, dtype=np.float32), "robot36")
        for _ in range(10):
            e.process(QUIET)
        e.ptt_up()                     # отпускание пробела не должно рвать картинку
        assert e.tx_state == SSTV
        getattr(e, stop)()
        assert e.tx_state == IDLE
        assert not e.process(QUIET).any()


def test_sstv_refused_when_busy_or_too_long():
    e = make_engine(tx_tot_s=2)
    assert "TOT" in e.start_sstv(np.zeros(RATE * 3, dtype=np.float32), "robot36")
    e.ptt_down()
    assert e.start_sstv(np.zeros(160, dtype=np.float32), "robot36") == "передатчик занят"
