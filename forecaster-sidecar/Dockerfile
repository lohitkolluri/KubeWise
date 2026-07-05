FROM python:3.12-slim

WORKDIR /sidecar

# Install system deps for numpy/scipy build (minimal).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forecaster_pb2.py forecaster_pb2_grpc.py server.py ./

EXPOSE 50051
ENV FORECASTER_PORT=50051

CMD ["python", "-u", "server.py"]
