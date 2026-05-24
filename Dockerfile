FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY app.py .
EXPOSE 3000
CMD ["python", "app.py"]
