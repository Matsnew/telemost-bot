#!/usr/bin/env bash
set -e

rm -f /tmp/.X99-lock /tmp/pulse-*

echo "[entrypoint] Starting Xvfb on :99 …"
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
sleep 1

echo "[entrypoint] Starting PulseAudio …"
# Run as root with --system flag disabled, use user daemon mode
export HOME=/root
export XDG_RUNTIME_DIR=/tmp/pulse-runtime
mkdir -p $XDG_RUNTIME_DIR
pulseaudio --start --exit-idle-time=-1 --log-level=error \
  --daemonize=yes \
  -n --load="module-native-protocol-unix" \
  --load="module-null-sink sink_name=virtual_sink" || \
pulseaudio --start --exit-idle-time=-1 --log-level=error || true
sleep 2

export DISPLAY=:99
export PULSE_RUNTIME_PATH=$XDG_RUNTIME_DIR

echo "[entrypoint] Starting application …"
exec python main.py
