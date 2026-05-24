FROM python:3.12-slim

WORKDIR /app

# Install system deps: nodejs for yt-dlp JS runtime (required for YouTube since 2025.11.12)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python deps FIRST (layer cache friendly)
COPY requirements.txt .

# Install yt-dlp[default] which includes yt-dlp-ejs (the JS challenge solver).
# DO NOT install yt-dlp-invidious - it hijacks the YouTube extractor and
# routes through Invidious instances, causing "format not available" errors.
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .

# Use gunicorn (production WSGI server), not the Flask dev server.
# WEB_CONCURRENCY=1 is set by Render automatically based on instance size;
# we honour it via the shell form so the env-var is expanded at runtime.
CMD gunicorn --bind 0.0.0.0:${PORT:-10000} --workers ${WEB_CONCURRENCY:-1} --timeout 120 app:app