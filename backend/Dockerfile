FROM node:24-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 DATA_DIR=/tmp/mediaharbor-youtube
WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/youtube_server.py ./youtube_server.py
RUN useradd --create-home downloader
USER downloader
EXPOSE 8080
CMD ["python", "youtube_server.py"]
