FROM python:3.11-slim

# ── System dependencies ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Browser
    chromium \
    chromium-driver \
    # Virtual display
    xvfb \
    # Audio
    pulseaudio \
    pulseaudio-utils \
    libpulse0 \
    # Audio processing
    ffmpeg \
    # Build tools (for asyncpg, cryptography)
    gcc \
    libpq-dev \
    # Misc
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (uses system chromium to avoid redundancy)
RUN playwright install-deps chromium \
    && playwright install chromium

# ── Application ────────────────────────────────────────────────────────────
COPY . .
RUN chmod +x entrypoint.sh

# PulseAudio config: allow connections from any user, disable auth
RUN mkdir -p /etc/pulse && printf '\
[daemon]\n\
exit-idle-time = -1\n\
\n\
[default]\n\
default-sample-rate = 16000\n\
default-sample-channels = 1\n' > /etc/pulse/daemon.conf

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
