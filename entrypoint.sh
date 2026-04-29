#!/usr/bin/env bash
set -e

rm -f /tmp/.X99-lock

echo "[entrypoint] Starting Xvfb on :99 …"
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
sleep 1

echo "[entrypoint] Starting D-Bus …"
mkdir -p /run/dbus
dbus-uuidgen > /etc/machine-id 2>/dev/null || true
dbus-daemon --system --fork 2>/dev/null || true
sleep 1

echo "[entrypoint] Starting PulseAudio …"
pulseaudio --system --disallow-exit --exit-idle-time=-1 \
  --log-level=error &
sleep 2

echo "[entrypoint] Loading virtual null sink …"
pactl load-module module-null-sink sink_name=virtual_sink \
  sink_properties=device.description=VirtualSink 2>/dev/null || true

export DISPLAY=:99
export PULSE_SERVER=unix:/run/pulse/native

echo "[entrypoint] Starting application …"
exec python main.py
