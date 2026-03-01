# Use Python 3.11 Slim as base
FROM python:3.11-slim

# Install system dependencies for FFmpeg, Aria2, and Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    git \
    gcc \
    curl \
    python3-dev \
    # Chromium system deps for Playwright
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    fonts-liberation \
    xvfb \
    # Node.js for the PO Token server
    && curl -sL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Ensure pip is up to date and use it to install requirements
COPY requirements.txt .
RUN python3 -m pip install --upgrade pip && \
    python3 -m pip install --no-cache-dir -r requirements.txt

# 1DM Hardening: Explicitly install playwright again and then Chromium
# Use python3 -m to be absolutely certain we are in the same environment
RUN python3 -m pip install playwright && \
    python3 -m playwright install chromium

# Copy project files
COPY . .

# Ensure DOWNLOADS directory exists
RUN mkdir -p DOWNLOADS

# Set environment variables
ENV FFMPEG_PATH=/usr/bin/ffmpeg
ENV PYTHONUNBUFFERED=1

# Koyeb expects a web service on port 8080
EXPOSE 8080

# Start everything via bot.py
CMD ["python3", "bot.py"]
