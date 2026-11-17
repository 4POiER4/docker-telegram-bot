FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN install -d -o 10001 -g 10001 -m 0750 /data \
    && install -d -m 0755 /snapshot \
    && touch /data/.keep /snapshot/.keep
COPY common.py collector.py bot.py ./

USER 10001:10001
CMD ["python", "bot.py"]
