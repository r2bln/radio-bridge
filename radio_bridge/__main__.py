import logging

from aiohttp import web

from .config import Config
from .server import build_app
from .tls import ssl_context


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    host, port = cfg.host_port
    ctx = ssl_context(cfg.data_dir, cfg.tls_san) if cfg.tls else None
    logging.getLogger("radio_bridge").info(
        "AUDIO_DEVICE=%s, %s://%s:%d, TOT %d с", cfg.audio_device, "https" if ctx else "http", host, port, cfg.tx_tot_s)
    web.run_app(build_app(cfg), host=host, port=port, ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
