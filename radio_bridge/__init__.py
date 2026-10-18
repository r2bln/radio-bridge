__version__ = "0.1.0"

# Мажорная версия протокола WebSocket: клиент сверяет её с полем `version` в `hello`.
PROTOCOL_VERSION = 1

RATE = 8000          # Гц на проводе и в движке
FRAME_MS = 20
FRAME = RATE * FRAME_MS // 1000  # 160 сэмплов = 160 байт μ-law
