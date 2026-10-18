import numpy as np
import pytest

from radio_bridge import FRAME, ulaw
from radio_bridge.config import Config
from radio_bridge.engine import IDLE, LOCKED, VOICE, WAKE, Engine, dbfs

QUIET = np.zeros(FRAME, dtype=np.float32)


def loud(db=-20.0):
    t = np.arange(FRAME)
    return (10 ** (db / 20) * np.sqrt(2) * np.sin(2 * np.pi * 800 * t / 8000)).astype(np.float32)


VOICE_FRAME = ulaw.encode(loud(-10))


def make(**kw):
    cfg = Config(**{"tx_tot_s": 2, "vox_tone_ms": 100, "tx_watchdog_ms": 100, "tx_tail_ms": 100,
                    "rx_hang_ms": 100, "rx_preroll_ms": 60, **kw})
    return Engine(cfg)


def kinds(events, kind):
    return [e for e in events if e.kind == kind]


def test_idle_output_is_digital_silence():
    e = make()
    for _ in range(50):
        out = e.process(loud(-5))
        assert not out.any()


def test_rx_never_leaks_to_tx_output():
    e = make()
    e.ptt_down()
    for _ in range(20):
        e.push_voice(ulaw.encode(QUIET))  # клиент шлёт тишину
        out = e.process(loud(-3))         # на входе громкий приём
        if e.tx_state == VOICE:
            assert np.max(np.abs(out)) < 1e-3


def test_wake_tone_then_voice():
    e = make()
    e.ptt_down()
    assert e.tx_state == WAKE
    outs = []
    for _ in range(5):  # 100 мс тона = 5 кадров
        e.push_voice(VOICE_FRAME)
        outs.append(e.process(QUIET))
    assert all(dbfs(o) > -20 for o in outs[1:-1])
    e.push_voice(VOICE_FRAME)
    e.process(QUIET)
    assert e.tx_state == VOICE
    out = e.process(QUIET)  # голос, накопленный во время тона
    assert dbfs(out) == pytest.approx(-10, abs=1.0)


def test_ptt_up_returns_silence():
    e = make()
    e.ptt_down()
    for _ in range(8):
        e.push_voice(VOICE_FRAME)
        e.process(QUIET)
    e.ptt_up()
    assert e.tx_state == IDLE
    assert not e.process(QUIET).any()
    e.push_voice(VOICE_FRAME)  # голос после отпускания выбрасывается
    assert not e.process(QUIET).any()


def test_watchdog_locks_when_client_stops_sending():
    e = make()
    e.ptt_down()
    for _ in range(10):
        e.push_voice(VOICE_FRAME)
        e.process(QUIET)
    for _ in range(6):  # 100 мс без кадров + запас
        out = e.process(QUIET)
    assert e.tx_state == LOCKED
    assert not out.any()
    e.push_voice(VOICE_FRAME)
    assert not e.process(QUIET).any()
    e.ptt_down()  # в LOCKED новое нажатие игнорируется
    assert e.tx_state == LOCKED
    e.ptt_up()
    assert e.tx_state == IDLE
    e.ptt_down()
    assert e.tx_state == WAKE


def test_tot_expires():
    e = make(tx_tot_s=1)
    e.ptt_down()
    events = []
    for _ in range(60):
        e.push_voice(VOICE_FRAME)
        e.process(QUIET)
        events += e.drain_events()
    assert e.tx_state == LOCKED
    assert len(kinds(events, "tot_expired")) == 1
    assert not e.process(QUIET).any()


def test_client_gone_stops_tx():
    e = make()
    e.ptt_down()
    for _ in range(8):
        e.push_voice(VOICE_FRAME)
        e.process(QUIET)
    e.client_gone()
    assert e.tx_state == IDLE
    assert not e.process(QUIET).any()


def test_rx_gate_preroll_and_hang():
    e = make()
    for _ in range(10):
        e.process(QUIET)
    assert not kinds(e.drain_events(), "rx")
    e.process(loud(-20))
    ev = e.drain_events()
    assert e.rx_open
    assert len(kinds(ev, "rx")) == 1 + 3  # пре-ролл 60 мс = 3 кадра + текущий
    for _ in range(4):
        e.process(QUIET)
    assert e.rx_open  # хвост 100 мс = 5 кадров
    e.process(QUIET)
    assert not e.rx_open
    e.drain_events()
    e.process(QUIET)
    assert not kinds(e.drain_events(), "rx")


def test_rx_suppressed_during_tx_and_tail():
    e = make()
    e.ptt_down()
    for _ in range(8):
        e.push_voice(VOICE_FRAME)
        e.process(loud(-20))
    assert not kinds(e.drain_events(), "rx")
    e.ptt_up()
    for _ in range(4):  # хвост TX 100 мс
        e.process(loud(-20))
    assert not kinds(e.drain_events(), "rx")
    for _ in range(3):
        e.process(loud(-20))
    assert kinds(e.drain_events(), "rx")


def test_rx_limiter_keeps_peaks_below_full_scale():
    e = make()
    e.process(np.full(FRAME, 1.5, dtype=np.float32))
    rx = kinds(e.drain_events(), "rx")
    assert rx and np.max(np.abs(ulaw.decode(rx[-1].data))) < 0.95
