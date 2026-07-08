# syntax=docker/dockerfile:1

# ── Build stage: download pre-built wheels (no compiler needed) ──────────────
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels \
    --only-binary :all: -r requirements.txt

# ── Runtime stage: install pre-built wheels, no compiler toolchain ─────────
FROM python:3.12-slim
WORKDIR /sidecar

RUN groupadd -r forecaster && useradd -r -g forecaster -u 10001 forecaster \
    && apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

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
