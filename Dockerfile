FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --home-dir /app bot \
    && mkdir -p /data/logs /data/media \
    && chown -R bot:bot /data

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY listener.py broadcaster.py admin_bot.py amazon_poller.py ./

USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["python", "listener.py", "--non-interactive"]
