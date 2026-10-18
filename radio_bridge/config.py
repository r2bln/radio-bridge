"""Конфигурация из переменных окружения (таблица в docs/SPEC.md §9)."""
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass
class Config:
    audio_device: str = "fake"
    listen: str = "0.0.0.0:8443"
    tls: bool = True
    tls_san: list[str] = field(default_factory=list)
    data_dir: Path = Path("data")

    rx_gate_db: float = -45.0
    rx_hang_ms: int = 1500
    rx_preroll_ms: int = 200
    rx_gain_db: float = 0.0

    tx_gain_db: float = 0.0
    vox_tone_hz: float = 1000.0
    vox_tone_ms: int = 500
    vox_tone_dbfs: float = -12.0
    tx_watchdog_ms: int = 1000
    tx_tail_ms: int = 1500
    tx_tot_s: int = 180
    tx_max_buffer_ms: int = 1000
    tx_dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        for f in fields(cls):
            name = f.name.upper()
            cur = getattr(c, f.name)
            if f.name == "tls_san":
                raw = os.environ.get("TLS_SAN", "")
                c.tls_san = [s.strip() for s in raw.split(",") if s.strip()]
            elif isinstance(cur, Path):
                c.data_dir = Path(_env(name, str(cur)))
            else:
                setattr(c, f.name, _env(name, cur, type(cur)))
        return c

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.listen.rpartition(":")
        return host or "0.0.0.0", int(port)
