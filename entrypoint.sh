#!/usr/bin/env bash
set -e

# Remove stale Xvfb lock if container restarted
rm -f /tmp/.X99-lock

echo "[entrypoint] Starting Xvfb on :99 …"
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
sleep 1

echo "[entrypoint] Starting PulseAudio …"
# --system allows running as root (required in Docker)
pulseaudio --system --disallow-exit --disallow-module-loading=false --exit-idle-time=-1 &
sleep 2

echo "[entrypoint] Loading virtual null sink …"
pactl --server=unix:/run/pulse/native load-module module-null-sink \
  sink_name=virtual_sink sink_properties=device.description=VirtualSink || true

export DISPLAY=:99
export PULSE_SERVER=unix:/run/pulse/native

echo "[entrypoint] Starting application …"
exec python main.py
