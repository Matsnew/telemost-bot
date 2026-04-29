#!/usr/bin/env bash
set -e

echo "[entrypoint] Starting Xvfb on :99 …"
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
sleep 1

echo "[entrypoint] Starting PulseAudio …"
pulseaudio --start --exit-idle-time=-1
sleep 1

echo "[entrypoint] Loading virtual null sink …"
pactl load-module module-null-sink sink_name=virtual_sink \
  sink_properties=device.description=VirtualSink || true

export DISPLAY=:99
export PULSE_SERVER=unix:/run/pulse/native

echo "[entrypoint] Starting application …"
exec python main.py
