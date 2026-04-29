#!/usr/bin/env bash
set -e

rm -f /tmp/.X99-lock

echo "[entrypoint] Starting Xvfb on :99 …"
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
sleep 1

echo "[entrypoint] Starting PulseAudio …"
# Run PulseAudio in daemon mode without system mode and without dbus
# auth-anonymous=1 allows root to connect
pulseaudio --daemonize=yes --exit-idle-time=-1 --log-level=error \
  -n \
  --load="module-native-protocol-unix auth-anonymous=1 socket=/tmp/pulse.sock" \
  --load="module-null-sink sink_name=virtual_sink sink_properties=device.description=VirtualSink" \
  --load="module-always-sink" || true
sleep 2

export DISPLAY=:99
export PULSE_SERVER=unix:/tmp/pulse.sock

echo "[entrypoint] Starting application …"
exec python main.py
