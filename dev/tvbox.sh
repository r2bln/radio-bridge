#!/bin/bash
# Запуск radio-bridge на тестовом хосте tvbox (PipeWire, станция в гарнитурном разъёме).
#   dev/tvbox.sh start [dry]   — dry: TX_DRY_RUN=1, в рацию идёт только тишина, TX пишется в data/
#   dev/tvbox.sh stop | status | log
set -u
cd "$(dirname "$0")/.."
export XDG_RUNTIME_DIR=/run/user/$(id -u)
PAT='^([^ ]*/)?[.]venv/bin/python -m radio_bridge$'
IN=alsa_input.pci-0000_00_1b.0.analog-stereo
OUT=alsa_output.pci-0000_00_1b.0.analog-stereo

case "${1:-status}" in
  start)
    pkill -f "$PAT"
    while pgrep -f "$PAT" >/dev/null; do sleep 0.2; done
    # вход — гарнитурный, 0 dB (на 100% WirePlumber даёт Headset Mic Boost +30 dB)
    pactl set-source-port "$IN" analog-input-headset-mic
    pactl set-source-volume "$IN" 10%
    [ "${2:-}" = dry ] && DRY=1 || DRY=0
    # тон VOX и SSTV громче голоса: на этом пути (PortAudio → PipeWire) −12 dBFS едва будит VOX
    VOX_TONE_DBFS=-6 SSTV_DBFS=-6 AUDIO_DEVICE="sd:$IN,$OUT" TX_DRY_RUN=$DRY LISTEN=100.64.0.42:8443 TLS_SAN=tvbox,100.64.0.42 \
      DATA_DIR="$PWD/data" setsid nohup .venv/bin/python -m radio_bridge > server.log 2>&1 < /dev/null &
    sleep 3
    grep -v aiohttp.access server.log
    ;;
  stop)   pkill -f "$PAT" && echo stopped || echo "not running" ;;
  status) pgrep -af "$PAT" || echo "not running"; pactl list sink-inputs | grep -E "application.name|Sink:" ;;
  log)    grep -v aiohttp.access server.log | tail -30 ;;
esac
