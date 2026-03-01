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

# Ensure pip is up to date
RUN python3 -m pip install --upgrade pip

# HARDENED INSTALLATION: Bypassing requirements.txt to avoid any encoding/caching issues
# Installing directly in the Docker layer
RUN python3 -m pip install --no-cache-dir \
    fastapi \
    uvicorn \
    playwright \
    httpx \
    python-multipart \
    pyroblack \
    tgcrypto \
    aiohttp==3.9.5 \
    aiofiles==23.2.1 \
    motor==3.4.0 \
    pymongo==4.7.3 \
    dnspython==2.6.1 \
    flask==3.0.3 \
    gunicorn==22.0.0 \
    psutil==5.9.8 \
    filetype==1.2.0 \
    Pillow==10.3.0 \
    requests==2.32.3 \
    yt-dlp \
    bgutil-ytdlp-pot-provider \
    python-dotenv==1.0.1 \
    aria2p \
    waitress==3.0.0

# Install Playwright Chromium browser
RUN python3 -m playwright install chromium

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
