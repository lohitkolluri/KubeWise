FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Environment variables
ENV PORT=8000
ENV PROMETHEUS_URL=https://localhost:9090
ENV PYTHONUNBUFFERED=1

# Run the application with Gunicorn
CMD gunicorn --bind 0.0.0.0:$PORT app:app
