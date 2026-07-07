# syntax=docker/dockerfile:1

# ── Build dependencies ───────────────────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /sidecar

RUN groupadd -r forecaster && useradd -r -g forecaster -u 10001 forecaster \
    && apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY forecaster_pb2.py forecaster_pb2_grpc.py server.py test_server.py ./

ENV FORECASTER_PORT=50051 \
    FORECASTER_HEALTH_PORT=8081 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 50051 8081
USER 10001

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz')"

CMD ["python", "-u", "server.py"]
